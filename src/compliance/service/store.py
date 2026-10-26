"""Run storage and feedback persistence.

The API and the worker are separate deployment units, so completed runs must
survive a request handler.  Redis is the production store; an in-memory store is
kept for tests and for a single-process demo where Redis is unavailable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError

from compliance.config import settings
from compliance.contracts import (
    AdvisoryResult,
    FeedbackRequest,
    FeedbackResponse,
    FeedbackSummary,
    RunEnvelope,
    RunStatus,
)
from compliance.logging_setup import get_logger
from compliance.observability import metrics
from compliance.util import iso_now, json_dumps, json_loads, new_id

logger = get_logger(__name__)


class StoreUnavailable(RuntimeError):
    pass


@dataclass
class StoredRun:
    envelope: RunEnvelope
    trace: list[dict[str, Any]] = field(default_factory=list)
    request: dict[str, Any] | None = None


class RunStore(Protocol):
    async def save(self, run: StoredRun) -> None: ...
    async def get(self, run_id: str) -> StoredRun | None: ...
    async def update_status(
        self,
        run_id: str,
        status: RunStatus,
        *,
        progress: str | None = None,
        error: str | None = None,
        result: AdvisoryResult | None = None,
        trace: list[dict[str, Any]] | None = None,
    ) -> None: ...
    async def add_feedback(self, run_id: str, feedback: FeedbackRequest) -> FeedbackResponse: ...
    async def feedback_summary(self, limit: int = 20) -> FeedbackSummary: ...
    async def ping(self) -> bool: ...
    async def close(self) -> None: ...


class MemoryRunStore:
    """Single-process store used by tests and the inline demo path."""

    def __init__(self) -> None:
        self.runs: dict[str, StoredRun] = {}
        self.feedback: list[dict[str, Any]] = []

    async def save(self, run: StoredRun) -> None:
        self.runs[run.envelope.run_id] = run

    async def get(self, run_id: str) -> StoredRun | None:
        return self.runs.get(run_id)

    async def update_status(
        self,
        run_id: str,
        status: RunStatus,
        *,
        progress: str | None = None,
        error: str | None = None,
        result: AdvisoryResult | None = None,
        trace: list[dict[str, Any]] | None = None,
    ) -> None:
        stored = self.runs.get(run_id)
        if stored is None:
            return
        envelope = stored.envelope.model_copy(
            update={
                "status": status,
                "progress": progress if progress is not None else stored.envelope.progress,
                "error": error,
                "result": result if result is not None else stored.envelope.result,
                "updated_at": iso_now(),
            }
        )
        stored.envelope = envelope
        if trace is not None:
            stored.trace = trace
        self.runs[run_id] = stored

    async def add_feedback(self, run_id: str, feedback: FeedbackRequest) -> FeedbackResponse:
        feedback_id = new_id("fb")
        self.feedback.append(
            {
                "feedback_id": feedback_id,
                "run_id": run_id,
                "satisfactory": feedback.satisfactory,
                "reason": feedback.reason,
                "category": feedback.category,
                "recorded_at": iso_now(),
            }
        )
        metrics.FEEDBACK_TOTAL.labels(
            satisfactory=str(feedback.satisfactory).lower(), category=feedback.category
        ).inc()
        return FeedbackResponse(feedback_id=feedback_id, run_id=run_id)

    async def feedback_summary(self, limit: int = 20) -> FeedbackSummary:
        return _summarise(self.feedback[-limit:], len(self.feedback))

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        return None


class RedisRunStore:
    """Redis-backed store with a bounded TTL per run."""

    def __init__(self, url: str | None = None, *, ttl_s: int | None = None) -> None:
        self.url = url or settings.redis_url
        self.ttl_s = ttl_s or settings.run_ttl_s
        self._client = Redis.from_url(self.url, decode_responses=True)
        self._feedback_key = "compliance:feedback"

    async def ping(self) -> bool:
        try:
            return bool(await self._client.ping())
        except (RedisError, OSError):
            return False

    async def close(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _key(run_id: str) -> str:
        return f"compliance:run:{run_id}"

    async def save(self, run: StoredRun) -> None:
        key = self._key(run.envelope.run_id)
        payload = {
            "envelope": run.envelope.model_dump_json(),
            "trace": json_dumps(run.trace),
            "request": json_dumps(run.request) if run.request is not None else "",
        }
        pipe = self._client.pipeline()
        pipe.hset(key, mapping=payload)
        pipe.expire(key, self.ttl_s)
        await pipe.execute()

    async def get(self, run_id: str) -> StoredRun | None:
        try:
            data = await self._client.hgetall(self._key(run_id))
        except (RedisError, OSError) as exc:
            raise StoreUnavailable(str(exc)) from exc
        if not data:
            return None
        envelope = RunEnvelope.model_validate_json(data["envelope"])
        trace = json_loads(data.get("trace"), default=[]) or []
        request = json_loads(data.get("request"), default=None)
        return StoredRun(envelope=envelope, trace=trace, request=request)

    async def update_status(
        self,
        run_id: str,
        status: RunStatus,
        *,
        progress: str | None = None,
        error: str | None = None,
        result: AdvisoryResult | None = None,
        trace: list[dict[str, Any]] | None = None,
    ) -> None:
        stored = await self.get(run_id)
        if stored is None:
            return
        stored.envelope = stored.envelope.model_copy(
            update={
                "status": status,
                "progress": progress if progress is not None else stored.envelope.progress,
                "error": error,
                "result": result if result is not None else stored.envelope.result,
                "updated_at": iso_now(),
            }
        )
        if trace is not None:
            stored.trace = trace
        await self.save(stored)

    async def add_feedback(self, run_id: str, feedback: FeedbackRequest) -> FeedbackResponse:
        feedback_id = new_id("fb")
        record = {
            "feedback_id": feedback_id,
            "run_id": run_id,
            "satisfactory": feedback.satisfactory,
            "reason": feedback.reason,
            "category": feedback.category,
            "recorded_at": iso_now(),
        }
        pipe = self._client.pipeline()
        pipe.lpush(self._feedback_key, json_dumps(record))
        pipe.ltrim(self._feedback_key, 0, 999)
        await pipe.execute()
        metrics.FEEDBACK_TOTAL.labels(
            satisfactory=str(feedback.satisfactory).lower(), category=feedback.category
        ).inc()
        return FeedbackResponse(feedback_id=feedback_id, run_id=run_id)

    async def feedback_summary(self, limit: int = 20) -> FeedbackSummary:
        total = int(await self._client.llen(self._feedback_key))
        raw = await self._client.lrange(self._feedback_key, 0, max(0, limit - 1))
        records = [json_loads(item, default={}) for item in raw]
        return _summarise(records, total)

    async def queue_depth(self) -> int:
        return int(await self._client.xlen(settings.queue_stream))


def _summarise(records: list[dict[str, Any]], total: int) -> FeedbackSummary:
    satisfactory = sum(1 for item in records if item.get("satisfactory"))
    by_category: dict[str, int] = {}
    for item in records:
        category = str(item.get("category") or "other")
        by_category[category] = by_category.get(category, 0) + 1
    return FeedbackSummary(
        total=total,
        satisfactory=satisfactory,
        unsatisfactory=len(records) - satisfactory,
        satisfaction_rate=round(satisfactory / len(records), 4) if records else 0.0,
        by_category=by_category,
        recent=records,
    )


async def make_store(*, backend: str | None = None) -> RunStore:
    """Choose Redis when reachable, otherwise fall back to memory.

    The fallback is explicit and logged: a demo must not fail only because Redis
    is not running, but a worker never silently runs without durable state.
    """
    backend = backend or settings.queue_backend
    if backend == "inline":
        return MemoryRunStore()
    store = RedisRunStore()
    if await store.ping():
        return store
    await store.close()
    if backend == "redis":
        raise StoreUnavailable(f"Redis is not reachable at {settings.redis_url}")
    logger.warning(
        "run_store_fallback",
        extra={"event": "run_store_fallback", "fields": {"backend": "memory", "reason": "redis_unreachable"}},
    )
    return MemoryRunStore()


__all__ = [
    "MemoryRunStore",
    "RedisRunStore",
    "RunStore",
    "StoredRun",
    "StoreUnavailable",
    "make_store",
]
