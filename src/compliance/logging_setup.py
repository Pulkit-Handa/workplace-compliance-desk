from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from compliance.config import settings

correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="")
run_id_var: ContextVar[str] = ContextVar("run_id", default="")
agent_var: ContextVar[str] = ContextVar("agent", default="")
step_var: ContextVar[str] = ContextVar("step", default="")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "service": getattr(record, "service", settings.service_name),
            "logger": record.name,
            "event": getattr(record, "event", record.getMessage()),
            "correlation_id": getattr(record, "correlation_id", correlation_id_var.get()),
            "run_id": getattr(record, "run_id", run_id_var.get()),
            "agent": getattr(record, "agent", agent_var.get()),
            "step": getattr(record, "step", step_var.get()),
        }
        for key, value in getattr(record, "fields", {}).items():
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(service_name: str | None = None) -> None:
    if service_name:
        settings.service_name = service_name
    root = logging.getLogger()
    if getattr(root, "_compliance_configured", False):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())
    logging.getLogger("uvicorn.access").handlers = [handler]
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    for noisy in ("httpx", "httpcore", "faiss", "urllib3", "opentelemetry"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    root._compliance_configured = True  # type: ignore[attr-defined]


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    logger.info(event, extra={"event": event, "fields": fields})


@contextmanager
def log_context(
    *, correlation_id: str | None = None, run_id: str | None = None, agent: str | None = None
) -> Iterator[None]:
    tokens = []
    if correlation_id is not None:
        tokens.append((correlation_id_var, correlation_id_var.set(correlation_id)))
    if run_id is not None:
        tokens.append((run_id_var, run_id_var.set(run_id)))
    if agent is not None:
        tokens.append((agent_var, agent_var.set(agent)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)
