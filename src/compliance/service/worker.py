"""Queue worker.

One process owns one consumer name.  It reads a bounded batch, executes runs
concurrently, and acknowledges only after the result is durably stored.  A
failure is retried with backoff and then dead-lettered; it is never acknowledged
as if it had succeeded.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import signal

from prometheus_client import start_http_server

from compliance.config import settings
from compliance.contracts import RunStatus
from compliance.corpus.index import IndexStore, get_default_index
from compliance.llm.base import LLMClient
from compliance.llm.factory import get_llm_client
from compliance.logging_setup import configure_logging, get_logger, log_context
from compliance.observability import metrics
from compliance.service.cache import RunCache, make_cache
from compliance.service.queue import QueueEntry, RedisStreamQueue, RunQueue
from compliance.service.runner import execute_run
from compliance.service.store import RunStore, make_store

logger = get_logger(__name__)


class Worker:
    def __init__(
        self,
        *,
        queue: RunQueue,
        store: RunStore,
        cache: RunCache,
        index: IndexStore | None = None,
        llm: LLMClient | None = None,
        consumer: str | None = None,
        concurrency: int | None = None,
    ) -> None:
        self.queue = queue
        self.store = store
        self.cache = cache
        self.index = index or get_default_index()
        self.llm = llm or get_llm_client()
        self.consumer = consumer or f"{settings.queue_consumer_prefix}-{os.getpid()}"
        self.concurrency = concurrency or settings.worker_concurrency
        self._stop = asyncio.Event()
        self._semaphore = asyncio.Semaphore(self.concurrency)
        self._inflight: set[asyncio.Task] = set()

    def stop(self) -> None:
        self._stop.set()

    async def handle(self, entry: QueueEntry) -> None:
        async with self._semaphore:
            metrics.ACTIVE_RUNS.inc()
            try:
                with log_context(run_id=entry.run_id):
                    await execute_run(
                        run_id=entry.run_id,
                        correlation_id=str(entry.payload.get("correlation_id") or ""),
                        payload=entry.payload,
                        store=self.store,
                        cache=self.cache,
                        index=self.index,
                        llm=self.llm,
                    )
                await self.queue.ack(entry)
            except Exception as exc:
                logger.exception(
                    "worker_run_failed",
                    extra={
                        "event": "worker_run_failed",
                        "fields": {
                            "run_id": entry.run_id,
                            "attempt": entry.attempt,
                            "error": str(exc)[:500],
                        },
                    },
                )
                await self.store.update_status(
                    entry.run_id,
                    RunStatus.FAILED,
                    progress="retrying" if entry.attempt < settings.queue_max_retries else "dead_lettered",
                    error=f"{type(exc).__name__}: {exc}"[:1000],
                )
                await self.queue.retry(entry, str(exc))
            finally:
                metrics.ACTIVE_RUNS.dec()

    async def run_forever(self) -> None:
        reclaimed = await self.queue.reclaim(consumer=self.consumer)
        for entry in reclaimed:
            await self.handle(entry)
        logger.info(
            "worker_started",
            extra={
                "event": "worker_started",
                "fields": {
                    "consumer": self.consumer,
                    "concurrency": self.concurrency,
                    "stream": settings.queue_stream,
                    "group": settings.queue_group,
                },
            },
        )
        while not self._stop.is_set():
            try:
                entries = await self.queue.read(consumer=self.consumer)
            except Exception as exc:
                logger.warning(
                    "queue_read_failed",
                    extra={"event": "queue_read_failed", "fields": {"error": str(exc)[:300]}},
                )
                await asyncio.sleep(1.0)
                continue
            for entry in entries:
                task = asyncio.create_task(self.handle(entry))
                self._inflight.add(task)
                task.add_done_callback(self._inflight.discard)
            with contextlib.suppress(Exception):
                metrics.QUEUE_DEPTH.set(await self.queue.depth())
            with contextlib.suppress(Exception):
                metrics.QUEUE_PENDING.set(await self.queue.pending())
        if self._inflight:
            await asyncio.gather(*self._inflight, return_exceptions=True)
        logger.info("worker_stopped", extra={"event": "worker_stopped", "fields": {"consumer": self.consumer}})

    async def run_once(self) -> int:
        """Process at most one batch; used by tests and the CLI."""
        entries = await self.queue.read(consumer=self.consumer, block_ms=100)
        for entry in entries:
            await self.handle(entry)
        return len(entries)


async def build_worker() -> tuple[Worker, RunStore, RunCache, IndexStore]:
    configure_logging(settings.service_name)
    index = get_default_index()
    store = await make_store()
    cache = await make_cache()
    queue = RedisStreamQueue()
    if not await queue.ping():
        raise RuntimeError(f"worker requires Redis at {settings.redis_url}")
    worker = Worker(queue=queue, store=store, cache=cache, index=index)
    return worker, store, cache, index


async def main() -> None:
    settings.ensure_dirs()
    worker, store, cache, index = await build_worker()
    with contextlib.suppress(OSError):
        start_http_server(settings.worker_metrics_port)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, worker.stop)
    try:
        await worker.run_forever()
    finally:
        await worker.queue.close()
        await store.close()
        await cache.close()
        await worker.llm.aclose()


__all__ = ["Worker", "build_worker", "main"]
