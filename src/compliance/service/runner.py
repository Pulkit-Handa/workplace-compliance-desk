"""Shared execution path for the API and the worker."""
from __future__ import annotations

import time
from typing import Any

from pydantic import ValidationError

from compliance.agents.graph import run_advisory
from compliance.config import settings
from compliance.contracts import (
    AdvisoryResult,
    ComplianceRequest,
    RunEnvelope,
    RunStatus,
)
from compliance.corpus.index import IndexStore, get_default_index
from compliance.llm.base import LLMClient
from compliance.llm.factory import get_llm_client
from compliance.llm.prompts import PromptRegistry
from compliance.logging_setup import get_logger, log_context
from compliance.observability import metrics
from compliance.service.cache import RunCache, cache_key
from compliance.service.store import RunStore, StoredRun
from compliance.util import iso_now, new_id

logger = get_logger(__name__)


def make_envelope(run_id: str, correlation_id: str, status: RunStatus = RunStatus.QUEUED) -> RunEnvelope:
    now = iso_now()
    return RunEnvelope(
        run_id=run_id,
        correlation_id=correlation_id,
        status=status,
        progress="queued",
        created_at=now,
        updated_at=now,
    )


def normalise_payload(request: ComplianceRequest | dict[str, Any]) -> dict[str, Any]:
    if isinstance(request, ComplianceRequest):
        return request.model_dump(mode="json")
    return ComplianceRequest.model_validate(request).model_dump(mode="json")


async def cache_lookup(
    payload: dict[str, Any],
    *,
    cache: RunCache,
    prompts: PromptRegistry,
    variant: str,
    index_version: str | None,
) -> AdvisoryResult | None:
    if not settings.run_cache_enabled:
        return None
    key = cache_key(
        payload,
        variant=variant,
        index_version=index_version,
        prompt_versions=prompts.versions(variant),
    )
    return await cache.get(key)


async def execute_run(
    *,
    run_id: str,
    correlation_id: str,
    payload: dict[str, Any],
    store: RunStore,
    cache: RunCache,
    variant: str | None = None,
    index: IndexStore | None = None,
    llm: LLMClient | None = None,
    prompts: PromptRegistry | None = None,
) -> AdvisoryResult:
    variant = variant or settings.prompt_variant
    index = index or get_default_index()
    prompts = prompts or PromptRegistry(variant=variant)
    llm = llm or get_llm_client()
    if isinstance(payload.get("request"), dict):
        request_payload = payload["request"]
        variant = str(payload.get("variant") or variant)
        prompts = PromptRegistry(variant=variant)
    else:
        request_payload = payload
    try:
        request = ComplianceRequest.model_validate(request_payload)
    except ValidationError as exc:
        await store.update_status(
            run_id,
            RunStatus.FAILED,
            progress="rejected",
            error=f"payload no longer validates: {exc.errors()[:2]}",
        )
        raise

    await store.update_status(run_id, RunStatus.RUNNING, progress="research")
    stored = await store.get(run_id)
    if stored is None:
        stored = StoredRun(envelope=make_envelope(run_id, correlation_id, RunStatus.RUNNING))
        stored.envelope = stored.envelope.model_copy(update={"progress": "research"})
        await store.save(stored)

    cached = await cache_lookup(
        request_payload,
        cache=cache,
        prompts=prompts,
        variant=variant,
        index_version=index.index_version,
    )
    if cached is not None:
        now = iso_now()
        result = cached.model_copy(
            update={
                "run_id": run_id,
                "correlation_id": correlation_id,
                "cached": True,
                "latency_ms": 0.0,
                "created_at": now,
                "completed_at": now,
            }
        )
        trace = [
            {
                "seq": 1,
                "ts": now,
                "elapsed_ms": 0.0,
                "kind": "cache_hit",
                "agent": None,
                "name": "whole_run_cache",
                "data": {"source_run_id": cached.run_id, "variant": variant},
            }
        ]
        await store.save(StoredRun(envelope=stored.envelope, trace=trace, request=request_payload))
        await store.update_status(
            run_id,
            result.status,
            progress="completed (cached)",
            result=result,
            trace=trace,
        )
        metrics.RUNS_CACHED_TOTAL.inc()
        logger.info(
            "run_cache_hit",
            extra={
                "event": "run_cache_hit",
                "fields": {"run_id": run_id, "source_run_id": cached.run_id, "variant": variant},
            },
        )
        return result

    started = time.perf_counter()
    trace: list[dict[str, Any]] = []
    with log_context(correlation_id=correlation_id, run_id=run_id):
        try:
            result = await run_advisory(
                request,
                run_id=run_id,
                correlation_id=correlation_id,
                variant=variant,
                index=index,
                llm=llm,
                prompts=prompts,
                trace_sink=trace,
            )
        except Exception as exc:
            await store.update_status(
                run_id,
                RunStatus.FAILED,
                progress="failed",
                error=f"{type(exc).__name__}: {exc}"[:1000],
                trace=trace,
            )
            logger.exception(
                "run_failed",
                extra={
                    "event": "run_failed",
                    "fields": {"run_id": run_id, "variant": variant, "error": str(exc)[:500]},
                },
            )
            raise

    await store.save(StoredRun(envelope=stored.envelope, trace=trace, request=request_payload))
    await store.update_status(
        run_id,
        result.status,
        progress="completed" if result.status is RunStatus.COMPLETED else "escalated",
        result=result,
        trace=trace,
    )
    if settings.run_cache_enabled and not result.degraded:
        key = cache_key(
            request_payload,
            variant=variant,
            index_version=index.index_version,
            prompt_versions=prompts.versions(variant),
        )
        await cache.set(key, result)
    logger.info(
        "run_executed",
        extra={
            "event": "run_executed",
            "fields": {
                "run_id": run_id,
                "variant": variant,
                "status": result.status.value,
                "route": result.route.value,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        },
    )
    return result


def new_run_ids() -> tuple[str, str]:
    return new_id("run"), new_id("corr")


__all__ = [
    "cache_lookup",
    "execute_run",
    "make_envelope",
    "new_run_ids",
    "normalise_payload",
]
