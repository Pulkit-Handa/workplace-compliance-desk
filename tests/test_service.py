"""Service-layer tests: storage, whole-run cache, token bucket, and queue.

Redis is the production backend, but these tests exercise the contracts with the
in-memory implementations that the API uses for the single-process demo.  The
fallback is explicit and must behave identically for callers.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from compliance.contracts import (
    AdvisoryResult,
    Domain,
    FeedbackRequest,
    Route,
    RouteDecision,
    RunEnvelope,
    RunStatus,
    VerificationOutcome,
    VerificationResult,
)
from compliance.service.cache import MemoryRunCache, cache_key, make_cache
from compliance.service.queue import InlineQueue, QueueEntry, RedisStreamQueue, make_queue
from compliance.service.ratelimit import CompositeLimiter, TokenBucketLimiter
from compliance.service.runner import make_envelope, normalise_payload
from compliance.service.store import MemoryRunStore, StoredRun

REPO_ROOT = Path(__file__).resolve().parents[1]


def _result(run_id: str = "run_1") -> AdvisoryResult:
    return AdvisoryResult(
        run_id=run_id,
        correlation_id="corr_1",
        status=RunStatus.COMPLETED,
        route=Route.ANSWER_WITH_CITATION,
        domain=Domain.GRATUITY,
        advisory="A cited advisory.",
        summary="A summary.",
        verification=VerificationResult(
            outcome=VerificationOutcome.PASSED,
            claims_checked=1,
            claims_supported=1,
            checks=["citation_retrieved"],
        ),
        route_decision=RouteDecision(
            route=Route.ANSWER_WITH_CITATION,
            basis="test",
            confidence=0.9,
        ),
        created_at="2026-01-01T00:00:00.000Z",
        completed_at="2026-01-01T00:00:01.000Z",
    )


def _envelope(run_id: str = "run_1") -> RunEnvelope:
    return make_envelope(run_id, "corr_1")


@pytest.mark.asyncio
async def test_memory_store_roundtrip_and_status_update():
    store = MemoryRunStore()
    await store.save(StoredRun(envelope=_envelope(), trace=[], request={"question": "x"}))
    stored = await store.get("run_1")
    assert stored is not None
    assert stored.request == {"question": "x"}

    await store.update_status(
        "run_1",
        RunStatus.COMPLETED,
        progress="completed",
        result=_result(),
        trace=[{"seq": 1, "kind": "run_start"}],
    )
    stored = await store.get("run_1")
    assert stored.envelope.status is RunStatus.COMPLETED
    assert stored.envelope.progress == "completed"
    assert stored.envelope.result is not None
    assert stored.trace[0]["kind"] == "run_start"
    assert await store.get("missing") is None


@pytest.mark.asyncio
async def test_memory_store_feedback_and_summary():
    store = MemoryRunStore()
    response = await store.add_feedback(
        "run_1",
        FeedbackRequest(satisfactory=False, reason="Citation was incomplete.", category="citation"),
    )
    assert response.recorded is True
    assert response.feedback_id.startswith("fb_")
    summary = await store.feedback_summary()
    assert summary.total == 1
    assert summary.unsatisfactory == 1
    assert summary.satisfaction_rate == 0.0
    assert summary.by_category == {"citation": 1}
    assert summary.recent[0]["run_id"] == "run_1"


@pytest.mark.asyncio
async def test_memory_cache_roundtrip_and_miss():
    cache = MemoryRunCache()
    key = "compliance:cache:test"
    assert await cache.get(key) is None
    await cache.set(key, _result())
    cached = await cache.get(key)
    assert cached is not None
    assert cached.run_id == "run_1"


@pytest.mark.asyncio
async def test_memory_cache_is_bounded():
    cache = MemoryRunCache(max_entries=2)
    await cache.set("a", _result("a"))
    await cache.set("b", _result("b"))
    await cache.set("c", _result("c"))
    assert await cache.get("a") is None
    assert await cache.get("c") is not None


def test_cache_key_covers_every_input_that_changes_the_answer(monkeypatch):
    from compliance.service import cache as cache_module

    payload = {"question": "gratuity", "as_of_date": "2026-01-01"}
    base = cache_key(
        payload,
        variant="v2",
        index_version="idx-1",
        prompt_versions={"draft": "draft-v2"},
    )
    assert base.startswith("compliance:cache:")
    assert base == cache_key(
        payload,
        variant="v2",
        index_version="idx-1",
        prompt_versions={"draft": "draft-v2"},
    )
    assert base != cache_key(
        payload, variant="v1", index_version="idx-1", prompt_versions={"draft": "draft-v2"}
    )
    assert base != cache_key(
        payload, variant="v2", index_version="idx-2", prompt_versions={"draft": "draft-v2"}
    )
    assert base != cache_key(
        payload, variant="v2", index_version="idx-1", prompt_versions={"draft": "draft-v3"}
    )
    assert base != cache_key(
        {"question": "maternity"},
        variant="v2",
        index_version="idx-1",
        prompt_versions={"draft": "draft-v2"},
    )
    # A code-only change to the deterministic composer must invalidate the cache.
    monkeypatch.setattr(cache_module, "policy_fingerprint", lambda: "changed-policy")
    assert base != cache_key(
        payload,
        variant="v2",
        index_version="idx-1",
        prompt_versions={"draft": "draft-v2"},
    )


@pytest.mark.asyncio
async def test_inline_cache_factory_returns_memory_backend():
    cache = await make_cache(backend="inline")
    assert isinstance(cache, MemoryRunCache)
    assert await cache.ping() is True
    await cache.close()


@pytest.mark.asyncio
async def test_token_bucket_allows_burst_then_refuses_with_retry():
    limiter = TokenBucketLimiter(per_minute=60, burst=2)
    first = await limiter.allow("client-a")
    second = await limiter.allow("client-a")
    third = await limiter.allow("client-a")
    assert first.allowed is True
    assert second.allowed is True
    assert third.allowed is False
    assert third.retry_after_ms >= 1
    # A different client has its own bucket.
    assert (await limiter.allow("client-b")).allowed is True


@pytest.mark.asyncio
async def test_token_bucket_refills_over_time():
    limiter = TokenBucketLimiter(per_minute=60_000, burst=1)
    assert (await limiter.allow("client")).allowed is True
    assert (await limiter.allow("client")).allowed is False
    await asyncio.sleep(0.02)
    assert (await limiter.allow("client")).allowed is True


@pytest.mark.asyncio
async def test_composite_limiter_applies_the_local_floor():
    local = TokenBucketLimiter(per_minute=60, burst=1)
    composite = CompositeLimiter(shared=TokenBucketLimiter(per_minute=60, burst=10), local=local)
    assert (await composite.allow("client")).allowed is True
    decision = await composite.allow("client")
    assert decision.allowed is False
    assert decision.retry_after_ms >= 1


@pytest.mark.asyncio
async def test_inline_queue_executes_a_handler():
    seen: list[str] = []

    async def handler(entry: QueueEntry) -> None:
        seen.append(entry.run_id)

    queue = InlineQueue(handler)
    await queue.enqueue("run_1", {"request": {}})
    await queue.close()
    assert seen == ["run_1"]
    assert await queue.depth() == 0


@pytest.mark.asyncio
async def test_inline_queue_reads_nothing_and_pings():
    async def handler(entry: QueueEntry) -> None:  # pragma: no cover - not used
        return None

    queue = InlineQueue(handler)
    assert await queue.read(consumer="test") == []
    assert await queue.pending() == 0
    assert await queue.reclaim(consumer="test") == []
    assert await queue.ping() is True
    await queue.close()


class _FakeStreamClient:
    def __init__(self, *, groups: list[dict], length: int) -> None:
        self.groups = groups
        self.length = length

    async def xinfo_groups(self, stream: str) -> list[dict]:
        return self.groups

    async def xlen(self, stream: str) -> int:
        return self.length


@pytest.mark.asyncio
async def test_redis_queue_depth_uses_lag_plus_pending_not_retained_length():
    queue = RedisStreamQueue()
    queue._group_ready = True
    queue._client = _FakeStreamClient(
        groups=[{"name": queue.group, "lag": 2, "pending": 3}],
        length=100,
    )
    assert await queue.depth() == 5


@pytest.mark.asyncio
async def test_redis_queue_depth_falls_back_when_lag_is_unknown():
    queue = RedisStreamQueue()
    queue._group_ready = True
    queue._client = _FakeStreamClient(
        groups=[{"name": queue.group, "lag": None, "pending": 4}],
        length=10,
    )
    assert await queue.depth() == 6


@pytest.mark.asyncio
async def test_inline_queue_retries_then_dead_letters(monkeypatch):
    from compliance.config import settings

    monkeypatch.setattr(settings, "queue_max_retries", 1)
    monkeypatch.setattr(settings, "queue_retry_backoff_s", 0.0)
    calls: list[int] = []

    async def handler(entry: QueueEntry) -> None:
        calls.append(entry.attempt)
        if entry.attempt == 0:
            raise RuntimeError("transient")

    queue = InlineQueue(handler)
    entry = QueueEntry(entry_id="1", run_id="run_1", payload={}, attempt=0)
    # InlineQueue does not catch handler exceptions itself; the worker does.
    # Exercise retry directly to keep this test about queue semantics.
    retried = await queue.retry(entry, "transient")
    assert retried is True
    await queue.close()
    assert calls == [1]

    calls.clear()
    terminal = QueueEntry(entry_id="2", run_id="run_2", payload={}, attempt=1)
    assert await queue.retry(terminal, "still failing") is False
    await queue.close()
    assert calls == []


@pytest.mark.asyncio
async def test_inline_queue_factory_requires_a_handler():
    with pytest.raises(ValueError):
        await make_queue(handler=None, backend="inline")

    async def handler(entry: QueueEntry) -> None:  # pragma: no cover
        return None

    queue = await make_queue(handler=handler, backend="inline")
    assert isinstance(queue, InlineQueue)
    await queue.close()


def test_normalise_payload_produces_json_mode_dates():
    payload = json.loads(
        (REPO_ROOT / "evals" / "golden.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )["request"]
    normalised = normalise_payload(payload)
    assert normalised["as_of_date"] == payload.get("as_of_date", normalised["as_of_date"])
    assert isinstance(normalised["as_of_date"], str)
    assert normalised["employment"]["start_date"] == "2022-11-01"
    assert normalised["consent"] is True


def test_make_envelope_starts_queued():
    envelope = make_envelope("run_1", "corr_1")
    assert envelope.status is RunStatus.QUEUED
    assert envelope.progress == "queued"
    assert envelope.created_at == envelope.updated_at
