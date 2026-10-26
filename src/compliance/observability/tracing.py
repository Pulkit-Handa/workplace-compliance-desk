from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from compliance.config import settings
from compliance.logging_setup import get_logger

logger = get_logger(__name__)

_configured = False
_tracer = None


def setup_tracing(service_name: str) -> None:
    """Configure OpenTelemetry once per process.

    Without OTEL_EXPORTER_OTLP_ENDPOINT the SDK keeps its no-op tracer, which is
    deliberate: local tests and `docker compose` with tracing disabled still run.
    """
    global _configured, _tracer
    if _configured:
        return
    _configured = True
    endpoint = settings.otel_exporter_otlp_endpoint.strip()
    if not endpoint:
        from opentelemetry import trace

        _tracer = trace.get_tracer(service_name)
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

        resource = Resource.create({"service.name": service_name, "service.namespace": "compliance-desk"})
        provider = TracerProvider(
            resource=resource,
            sampler=ParentBased(TraceIdRatioBased(settings.otel_traces_sampler_arg)),
        )
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer(service_name)
        logger.info("otel_configured", extra={"event": "otel_configured", "fields": {"endpoint": endpoint}})
    except Exception as exc:  # pragma: no cover - observability must not stop the service
        from opentelemetry import trace

        _tracer = trace.get_tracer(service_name)
        logger.warning(
            "otel_setup_failed",
            extra={"event": "otel_setup_failed", "fields": {"error": str(exc)}},
        )


def get_tracer():
    global _tracer
    if _tracer is None:
        from opentelemetry import trace

        _tracer = trace.get_tracer(settings.service_name)
    return _tracer


@contextmanager
def span(name: str, attributes: dict[str, Any] | None = None) -> Iterator[Any]:
    tracer = get_tracer()
    with tracer.start_as_current_span(name) as current:
        for key, value in (attributes or {}).items():
            if value is not None:
                current.set_attribute(key, value)
        yield current


def instrument_fastapi(app) -> None:
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls="health/live,health/ready,metrics")
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "fastapi_instrumentation_failed",
            extra={"event": "fastapi_instrumentation_failed", "fields": {"error": str(exc)}},
        )


def instrument_httpx() -> None:
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "httpx_instrumentation_failed",
            extra={"event": "httpx_instrumentation_failed", "fields": {"error": str(exc)}},
        )


def instrument_redis() -> None:
    try:
        from opentelemetry.instrumentation.redis import RedisInstrumentor

        RedisInstrumentor().instrument()
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "redis_instrumentation_failed",
            extra={"event": "redis_instrumentation_failed", "fields": {"error": str(exc)}},
        )
