"""Durable run queue.

Redis Streams gives at-least-once delivery with a consumer group, explicit
acknowledgement, bounded retries, and a dead-letter stream.  The inline queue is
for a single-process demo and tests; it deliberately keeps the same interface so
the API never has two code paths for the payload.
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError, ResponseError

from compliance.config import settings
from compliance.logging_setup import get_logger
from compliance.observability import metrics
from compliance.util import iso_now, json_dumps, json_loads

logger = get_logger(__name__)


@dataclass
class QueueEntry:
    entry_id: str
    run_id: str
    payload: dict[str, Any]
    attempt: int
    delivered_at: str | None = None


class RunQueue(Protocol):
    async def enqueue(self, run_id: str, payload: dict[str, Any], *, attempt: int = 0) -> str: ...
    async def read(self, *, consumer: str, count: int | None = None, block_ms: int | None = None) -> list[QueueEntry]: ...
    async def ack(self, entry: QueueEntry) -> None: ...
    async def retry(self, entry: QueueEntry, error: str) -> bool: ...
    async def dead_letter(self, entry: QueueEntry, error: str) -> None: ...
    async def depth(self) -> int: ...
    async def pending(self) -> int: ...
    async def reclaim(self, *, consumer: str, min_idle_ms: int = 60_000) -> list[QueueEntry]: ...
    async def ping(self) -> bool: ...
    async def close(self) -> None: ...


class InlineQueue:
    """Run entries as background asyncio tasks in the API process."""

    def __init__(self, handler: Callable[[QueueEntry], Awaitable[None]]) -> None:
        self.handler = handler
        self._tasks: set[asyncio.Task] = set()
        self._depth = 0

    async def enqueue(self, run_id: str, payload: dict[str, Any], *, attempt: int = 0) -> str:
        entry = QueueEntry(
            entry_id=f"inline:{run_id}:{attempt}",
            run_id=run_id,
            payload=payload,
            attempt=attempt,
            delivered_at=iso_now(),
        )
        self._depth += 1
        task = asyncio.create_task(self._execute(entry))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return entry.entry_id

    async def _execute(self, entry: QueueEntry) -> None:
        try:
            await self.handler(entry)
        finally:
            self._depth = max(0, self._depth - 1)

    async def read(self, *, consumer: str, count: int | None = None, block_ms: int | None = None) -> list[QueueEntry]:
        return []

    async def ack(self, entry: QueueEntry) -> None:
        return None

    async def retry(self, entry: QueueEntry, error: str) -> bool:
        if entry.attempt >= settings.queue_max_retries:
            await self.dead_letter(entry, error)
            return False
        await asyncio.sleep(settings.queue_retry_backoff_s * (2**entry.attempt))
        await self.enqueue(entry.run_id, entry.payload, attempt=entry.attempt + 1)
        return True

    async def dead_letter(self, entry: QueueEntry, error: str) -> None:
        logger.error(
            "run_dead_lettered",
            extra={
                "event": "run_dead_lettered",
                "fields": {"run_id": entry.run_id, "attempt": entry.attempt, "error": error[:300]},
            },
        )

    async def depth(self) -> int:
        return self._depth

    async def pending(self) -> int:
        return 0

    async def reclaim(self, *, consumer: str, min_idle_ms: int = 60_000) -> list[QueueEntry]:
        return []

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)


class RedisStreamQueue:
    def __init__(
        self,
        url: str | None = None,
        *,
        stream: str | None = None,
        group: str | None = None,
        dlq: str | None = None,
    ) -> None:
        self._client = Redis.from_url(url or settings.redis_url, decode_responses=True)
        self.stream = stream or settings.queue_stream
        self.group = group or settings.queue_group
        self.dlq = dlq or settings.queue_dlq_stream
        self._group_ready = False

    async def _ensure_group(self) -> None:
        if self._group_ready:
            return
        try:
            await self._client.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        self._group_ready = True

    async def enqueue(self, run_id: str, payload: dict[str, Any], *, attempt: int = 0) -> str:
        await self._ensure_group()
        entry_id = await self._client.xadd(
            self.stream,
            {
                "run_id": run_id,
                "payload": json_dumps(payload),
                "attempt": str(attempt),
                "enqueued_at": iso_now(),
            },
            maxlen=100_000,
            approximate=True,
        )
        metrics.RUNS_QUEUED.inc()
        return str(entry_id)

    async def read(self, *, consumer: str, count: int | None = None, block_ms: int | None = None) -> list[QueueEntry]:
        await self._ensure_group()
        response = await self._client.xreadgroup(
            self.group,
            consumer,
            {self.stream: ">"},
            count=count or settings.queue_batch,
            block=settings.queue_block_ms if block_ms is None else block_ms,
        )
        return _parse_entries(response)

    async def ack(self, entry: QueueEntry) -> None:
        await self._client.xack(self.stream, self.group, entry.entry_id)

    async def retry(self, entry: QueueEntry, error: str) -> bool:
        if entry.attempt >= settings.queue_max_retries:
            await self.dead_letter(entry, error)
            return False
        await asyncio.sleep(min(8.0, settings.queue_retry_backoff_s * (2**entry.attempt)))
        await self.enqueue(entry.run_id, entry.payload, attempt=entry.attempt + 1)
        await self._client.xack(self.stream, self.group, entry.entry_id)
        return True

    async def dead_letter(self, entry: QueueEntry, error: str) -> None:
        await self._client.xadd(
            self.dlq,
            {
                "run_id": entry.run_id,
                "payload": json_dumps(entry.payload),
                "attempt": str(entry.attempt),
                "error": error[:1000],
                "dead_at": iso_now(),
            },
            maxlen=10_000,
            approximate=True,
        )
        await self._client.xack(self.stream, self.group, entry.entry_id)
        logger.error(
            "run_dead_lettered",
            extra={
                "event": "run_dead_lettered",
                "fields": {"run_id": entry.run_id, "attempt": entry.attempt, "error": error[:300]},
            },
        )

    async def depth(self) -> int:
        """Return the unprocessed backlog, not the retained stream length.

        ``XLEN`` never decreases because acknowledged entries stay in the
        stream until trimmed, so using it as queue depth makes the backlog
        alert fire permanently after a few requests.  The consumer-group
        ``lag`` counts entries that have not been delivered yet and ``pending``
        counts delivered-but-unacknowledged entries; their sum is the real
        backlog the worker must drain.
        """
        await self._ensure_group()
        try:
            groups = await self._client.xinfo_groups(self.stream)
        except ResponseError:
            return 0
        for group in groups:
            if group.get("name") == self.group:
                lag = group.get("lag")
                pending = int(group.get("pending") or 0)
                if lag is None:
                    # Lag is unavailable before the first read on older Redis
                    # versions; fall back to the retained length minus pending.
                    length = int(await self._client.xlen(self.stream))
                    return max(0, length - pending)
                return int(lag) + pending
        return 0

    async def pending(self) -> int:
        try:
            summary = await self._client.xpending(self.stream, self.group)
        except ResponseError:
            return 0
        return int(summary.get("pending", 0)) if isinstance(summary, dict) else int(summary or 0)

    async def reclaim(self, *, consumer: str, min_idle_ms: int = 60_000) -> list[QueueEntry]:
        await self._ensure_group()
        try:
            _cursor, messages, _deleted = await self._client.xautoclaim(
                self.stream,
                self.group,
                consumer,
                min_idle_time=min_idle_ms,
                start_id="0-0",
                count=settings.queue_batch,
            )
        except (RedisError, ResponseError):
            return []
        entries: list[QueueEntry] = []
        for entry_id, fields in messages:
            entries.append(_entry_from_fields(str(entry_id), fields))
        return entries

    async def ping(self) -> bool:
        try:
            return bool(await self._client.ping())
        except (RedisError, OSError):
            return False

    async def close(self) -> None:
        await self._client.aclose()


def _entry_from_fields(entry_id: str, fields: dict[str, Any]) -> QueueEntry:
    payload = json_loads(fields.get("payload"), default={}) or {}
    return QueueEntry(
        entry_id=entry_id,
        run_id=fields.get("run_id", ""),
        payload=payload,
        attempt=int(fields.get("attempt") or 0),
        delivered_at=fields.get("enqueued_at"),
    )


def _parse_entries(response: Any) -> list[QueueEntry]:
    entries: list[QueueEntry] = []
    if not response:
        return entries
    for _stream, messages in response:
        for entry_id, fields in messages:
            entries.append(_entry_from_fields(str(entry_id), fields))
    return entries


async def make_queue(
    *,
    handler: Callable[[QueueEntry], Awaitable[None]] | None = None,
    backend: str | None = None,
) -> RunQueue:
    backend = backend or settings.queue_backend
    if backend == "inline":
        if handler is None:
            raise ValueError("inline queue requires a handler")
        return InlineQueue(handler)
    queue = RedisStreamQueue()
    if await queue.ping():
        return queue
    await queue.close()
    if backend == "redis":
        raise RuntimeError(f"Redis is not reachable at {settings.redis_url}")
    if handler is None:
        raise RuntimeError("Redis is unreachable and no inline handler was supplied")
    logger.warning(
        "queue_fallback",
        extra={"event": "queue_fallback", "fields": {"backend": "inline", "reason": "redis_unreachable"}},
    )
    return InlineQueue(handler)


def queue_depth_metrics(queue: RunQueue) -> None:
    async def update() -> None:
        with contextlib.suppress(Exception):
            metrics.QUEUE_DEPTH.set(await queue.depth())
        with contextlib.suppress(Exception):
            metrics.QUEUE_PENDING.set(await queue.pending())

    return update


__all__ = [
    "InlineQueue",
    "QueueEntry",
    "RedisStreamQueue",
    "RunQueue",
    "make_queue",
    "queue_depth_metrics",
]
