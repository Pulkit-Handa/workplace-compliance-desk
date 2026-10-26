"""FastAPI service for the Workplace Compliance Advisory Desk."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from compliance.config import settings
from compliance.contracts import (
    AcceptedRun,
    FeedbackRequest,
    FeedbackResponse,
    FeedbackSummary,
    QuarantineResponse,
    RunEnvelope,
    RunStatus,
    RunTrace,
    TraceEvent,
)
from compliance.corpus.index import IndexStore, get_default_index
from compliance.guardrails import guardrail_disclaimer
from compliance.ingestion import IngestionOutcome, validate_payload
from compliance.llm.base import LLMClient
from compliance.llm.factory import get_llm_client
from compliance.llm.prompts import PromptRegistry
from compliance.logging_setup import configure_logging, get_logger, log_context
from compliance.observability import metrics
from compliance.observability.tracing import instrument_fastapi, setup_tracing
from compliance.service.cache import RunCache, make_cache
from compliance.service.queue import QueueEntry, RunQueue, make_queue
from compliance.service.ratelimit import make_limiter
from compliance.service.runner import (
    cache_lookup,
    execute_run,
    make_envelope,
    new_run_ids,
    normalise_payload,
)
from compliance.service.store import RunStore, StoredRun, make_store
from compliance.util import iso_now, new_id

logger = get_logger(__name__)

STATIC_DIR = Path(__file__).resolve().parents[2] / "web" / "static"


@dataclass
class Resources:
    index: IndexStore
    store: RunStore
    cache: RunCache
    queue: RunQueue
    limiter: Any
    llm: LLMClient
    prompts: PromptRegistry
    closeables: list[Any] = field(default_factory=list)


def _client_id_from_header(request: Request) -> str:
    value = request.headers.get("X-Client-Id", "anonymous")
    return "".join(ch for ch in value if ch.isalnum() or ch in "_.:-")[:64] or "anonymous"


def _variant_from_header(request: Request) -> str:
    requested = request.headers.get("X-Prompt-Variant", settings.prompt_variant)
    if requested not in {"v1", "v2"}:
        raise ValueError("X-Prompt-Variant must be v1 or v2")
    return requested


def _write_quarantine(payload: bytes, outcome: IngestionOutcome) -> str:
    quarantine_id = new_id("quar")
    directory = settings.quarantine_dir / date.today().isoformat()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{quarantine_id}.json"
    record = {
        "quarantine_id": quarantine_id,
        "correlation_id": outcome.correlation_id,
        "reason": outcome.reason,
        "field_errors": outcome.field_errors,
        "received_at": iso_now(),
        "body_sha256": __import__("hashlib").sha256(payload).hexdigest(),
        "body": payload.decode("utf-8", errors="replace")[:16_000],
    }
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return quarantine_id


async def _close_resource(item: Any) -> None:
    """Close a resource that may expose either ``aclose`` (LLM clients) or ``close``."""
    closer = getattr(item, "aclose", None) or getattr(item, "close", None)
    if closer is not None:
        await closer()


async def _build_resources(
    *,
    backend: str | None,
    index: IndexStore | None,
    store: RunStore | None,
    cache: RunCache | None,
    limiter: Any,
    llm: LLMClient | None,
) -> Resources:
    settings.ensure_dirs()
    owned: list[Any] = []
    if index is None:
        index = get_default_index()
    if store is None:
        store = await make_store(backend=backend)
        owned.append(store)
    if cache is None:
        cache = await make_cache(backend=backend)
        owned.append(cache)
    if limiter is None:
        limiter = await make_limiter(backend=backend)
        owned.append(limiter)
    if llm is None:
        llm = get_llm_client()
        owned.append(llm)
    prompts = PromptRegistry()
    variant = settings.prompt_variant

    async def handler(entry: QueueEntry) -> None:
        await execute_run(
            run_id=entry.run_id,
            correlation_id=str(entry.payload.get("correlation_id") or new_id("corr")),
            payload=entry.payload,
            store=store,
            cache=cache,
            variant=str(entry.payload.get("variant") or variant),
            index=index,
            llm=llm,
            prompts=PromptRegistry(variant=str(entry.payload.get("variant") or variant)),
        )

    queue = await make_queue(handler=handler, backend=backend)
    owned.append(queue)
    return Resources(
        index=index,
        store=store,
        cache=cache,
        queue=queue,
        limiter=limiter,
        llm=llm,
        prompts=prompts,
        closeables=owned,
    )


def create_app(
    *,
    backend: str | None = None,
    index: IndexStore | None = None,
    store: RunStore | None = None,
    cache: RunCache | None = None,
    limiter: Any = None,
    llm: LLMClient | None = None,
    static_dir: Path | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configure_logging(settings.service_name)
        setup_tracing(settings.service_name)
        resources = await _build_resources(
            backend=backend,
            index=index,
            store=store,
            cache=cache,
            limiter=limiter,
            llm=llm,
        )
        app.state.resources = resources
        logger.info(
            "api_started",
            extra={
                "event": "api_started",
                "fields": {
                    "index_version": resources.index.index_version,
                    "queue_backend": type(resources.queue).__name__,
                    "store_backend": type(resources.store).__name__,
                },
            },
        )
        try:
            yield
        finally:
            for item in reversed(resources.closeables):
                try:
                    await _close_resource(item)
                except Exception:  # shutdown must finish
                    logger.exception("resource_close_failed", extra={"event": "resource_close_failed"})
            logger.info("api_stopped", extra={"event": "api_stopped"})

    app = FastAPI(
        title="Workplace Compliance Advisory Desk",
        version="1.0.0",
        description=(
            "Cited HR compliance advisories with provision-aware retrieval, an in-force check, "
            "claim-by-claim verification, and escalation when the evidence is incomplete."
        ),
        lifespan=lifespan,
    )
    app.state.resources = None

    @app.middleware("http")
    async def correlation_middleware(request: Request, call_next):
        correlation_id = request.headers.get("X-Correlation-ID", "")
        if not correlation_id or len(correlation_id) > 64:
            correlation_id = new_id("corr")
        context = log_context(correlation_id=correlation_id)
        context.__enter__()
        request.state.correlation_id = correlation_id
        try:
            response = await call_next(request)
        finally:
            context.__exit__(None, None, None)
        response.headers["X-Correlation-ID"] = correlation_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.middleware("http")
    async def body_size_middleware(request: Request, call_next):
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > 64 * 1024:
            metrics.QUARANTINE_TOTAL.labels(reason="body_too_large").inc()
            return JSONResponse(
                status_code=413,
                content={
                    "error": "validation_failed",
                    "correlation_id": getattr(request.state, "correlation_id", ""),
                    "quarantine_id": "",
                    "field_errors": [
                        {"field": "body", "code": "too_large", "message": "body exceeds 64 KiB"}
                    ],
                    "agent_started": False,
                    "message": "Request rejected at the ingestion boundary. No agent run was started.",
                },
            )
        return await call_next(request)

    def resources(request: Request) -> Resources:
        value = request.app.state.resources
        if value is None:  # pragma: no cover - only before startup
            raise RuntimeError("application resources are not initialised")
        return value

    @app.get("/", include_in_schema=False)
    async def root():
        target = (static_dir or STATIC_DIR) / "index.html"
        if not target.exists():
            return JSONResponse({"service": "compliance-advisory-desk", "docs": "/docs"})
        return FileResponse(target)

    if (static_dir or STATIC_DIR).exists():
        app.mount(
            "/static",
            StaticFiles(directory=str(static_dir or STATIC_DIR)),
            name="static",
        )

    @app.post(
        "/v1/advisories",
        status_code=202,
        response_model=AcceptedRun,
        responses={200: {"model": AcceptedRun}, 422: {"model": QuarantineResponse}, 429: {}},
        tags=["advisories"],
    )
    async def create_advisory(request: Request, res: Resources = Depends(resources)):
        raw = await request.body()
        correlation_id = request.state.correlation_id
        outcome = validate_payload(raw, correlation_id=correlation_id)
        if not outcome.ok or outcome.request is None:
            quarantine_id = _write_quarantine(raw, outcome)
            metrics.QUARANTINE_TOTAL.labels(reason=outcome.reason or "validation").inc()
            logger.warning(
                "request_quarantined",
                extra={
                    "event": "request_quarantined",
                    "fields": {
                        "correlation_id": correlation_id,
                        "quarantine_id": quarantine_id,
                        "reason": outcome.reason,
                        "errors": outcome.field_errors[:5],
                    },
                },
            )
            payload = QuarantineResponse(
                correlation_id=correlation_id,
                quarantine_id=quarantine_id,
                field_errors=outcome.field_errors,
                message="Request rejected at the ingestion boundary. No agent run was started.",
            )
            return JSONResponse(status_code=422, content=payload.model_dump(mode="json"))

        advisory_request = outcome.request
        decision = await res.limiter.allow(advisory_request.requester.client_id)
        if not decision.allowed:
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limited",
                    "correlation_id": correlation_id,
                    "retry_after_ms": decision.retry_after_ms,
                },
                headers={"Retry-After": str(max(1, decision.retry_after_ms // 1000))},
            )
        try:
            variant = _variant_from_header(request)
        except ValueError as exc:
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_variant", "detail": str(exc), "correlation_id": correlation_id},
            )

        request_payload = normalise_payload(advisory_request)
        cached = await cache_lookup(
            request_payload,
            cache=res.cache,
            prompts=PromptRegistry(variant=variant),
            variant=variant,
            index_version=res.index.index_version,
        )
        run_id, _ = new_run_ids()
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
            envelope = make_envelope(run_id, correlation_id, RunStatus.QUEUED).model_copy(
                update={
                    "status": result.status,
                    "progress": "completed (cached)",
                    "result": result,
                    "updated_at": now,
                }
            )
            await res.store.save(StoredRun(envelope=envelope, trace=trace, request=request_payload))
            metrics.RUNS_CACHED_TOTAL.inc()
            return AcceptedRun(
                run_id=run_id,
                correlation_id=correlation_id,
                status=result.status,
                poll_url=f"/v1/advisories/{run_id}",
                estimated_seconds=0,
                cached=True,
            )

        envelope = make_envelope(run_id, correlation_id, RunStatus.QUEUED)
        await res.store.save(StoredRun(envelope=envelope, trace=[], request=request_payload))
        await res.queue.enqueue(
            run_id,
            {"request": request_payload, "variant": variant, "correlation_id": correlation_id},
        )
        logger.info(
            "run_accepted",
            extra={
                "event": "run_accepted",
                "fields": {
                    "run_id": run_id,
                    "correlation_id": correlation_id,
                    "client_id": advisory_request.requester.client_id,
                    "variant": variant,
                    "queue": type(res.queue).__name__,
                },
            },
        )
        return AcceptedRun(
            run_id=run_id,
            correlation_id=correlation_id,
            status=RunStatus.QUEUED,
            poll_url=f"/v1/advisories/{run_id}",
            estimated_seconds=15,
        )

    @app.get(
        "/v1/advisories/{run_id}",
        response_model=RunEnvelope,
        responses={404: {}},
        tags=["advisories"],
    )
    async def get_advisory(run_id: str, res: Resources = Depends(resources)):
        stored = await res.store.get(run_id)
        if stored is None:
            return JSONResponse(
                status_code=404,
                content={"error": "not_found", "run_id": run_id, "detail": "unknown or expired run"},
            )
        return stored.envelope

    @app.get(
        "/v1/advisories/{run_id}/trace",
        response_model=RunTrace,
        responses={404: {}},
        tags=["advisories"],
    )
    async def get_trace(run_id: str, res: Resources = Depends(resources)):
        stored = await res.store.get(run_id)
        if stored is None:
            return JSONResponse(
                status_code=404,
                content={"error": "not_found", "run_id": run_id, "detail": "unknown or expired run"},
            )
        events = [TraceEvent.model_validate(item) for item in stored.trace if isinstance(item, dict)]
        return RunTrace(
            run_id=run_id,
            correlation_id=stored.envelope.correlation_id,
            request=stored.request,
            events=events,
            result=stored.envelope.result,
        )

    @app.post(
        "/v1/advisories/{run_id}/feedback",
        response_model=FeedbackResponse,
        responses={404: {}},
        tags=["feedback"],
    )
    async def add_feedback(
        run_id: str,
        feedback: FeedbackRequest,
        res: Resources = Depends(resources),
    ):
        stored = await res.store.get(run_id)
        if stored is None:
            return JSONResponse(
                status_code=404,
                content={"error": "not_found", "run_id": run_id, "detail": "unknown or expired run"},
            )
        return await res.store.add_feedback(run_id, feedback)

    @app.get("/v1/feedback/summary", response_model=FeedbackSummary, tags=["feedback"])
    async def feedback_summary(res: Resources = Depends(resources)):
        return await res.store.feedback_summary()

    @app.get("/v1/index", tags=["operations"])
    async def index_metadata(res: Resources = Depends(resources)):
        return {
            **res.index.metadata(),
            "prompts": res.prompts.manifest(),
            "slo": {
                "p95_ms": settings.slo_p95_ms,
                "target_rps": settings.slo_target_rps,
                "declared_before_measurement": True,
            },
            "routing": {
                "answer_with_citation": "every operative claim passes verification",
                "request_more_establishment_detail": "the coverage threshold cannot be applied",
                "legal_counsel_review": "incomplete evidence, contested facts, or a prohibited determination",
            },
        }

    @app.get("/v1/prompts", tags=["operations"])
    async def prompts(res: Resources = Depends(resources)):
        return res.prompts.manifest()

    @app.get("/v1/demo/cases", tags=["demo"])
    async def demo_cases():
        from compliance.demo_cases import DEMO_CASES

        return {"cases": DEMO_CASES, "note": "All examples are synthetic."}

    @app.get("/health/live", include_in_schema=False)
    async def live():
        return {"status": "ok", "service": settings.service_name}

    @app.get("/health/ready", include_in_schema=False)
    async def ready(request: Request):
        res = resources(request)
        checks = {
            "index": res.index.index_version is not None,
            "store": await res.store.ping(),
            "queue": await res.queue.ping(),
            "cache": await res.cache.ping(),
            "limiter": await res.limiter.ping(),
        }
        ok = all(checks.values())
        body = {
            "status": "ok" if ok else "degraded",
            "checks": checks,
            "index_version": res.index.index_version,
        }
        return JSONResponse(status_code=200 if ok else 503, content=body)

    @app.get("/metrics", include_in_schema=False)
    async def metrics_endpoint():
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/v1/scope", tags=["operations"])
    async def scope():
        return {
            "in_scope": [
                "gratuity under the Code on Social Security, 2020",
                "maternity benefit under the Code on Social Security, 2020",
                "workplace harassment obligations under the POSH Act, 2013",
                "historical gratuity questions under the Payment of Gratuity Act, 1972",
            ],
            "out_of_scope": [
                "advice on a contested harassment complaint",
                "legal opinions, predictions, or findings of guilt",
                "minimum wages and other topics outside the indexed corpus",
            ],
            "disclaimer": guardrail_disclaimer(),
        }

    instrument_fastapi(app)
    return app


app = create_app()


__all__ = ["Resources", "app", "create_app"]
