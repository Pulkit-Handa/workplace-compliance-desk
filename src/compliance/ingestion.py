from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from compliance.config import settings
from compliance.contracts import ComplianceRequest
from compliance.guardrails import GuardrailResult, check_free_text, sanitise_mapping
from compliance.logging_setup import get_logger
from compliance.util import json_loads, new_id

logger = get_logger(__name__)

MAX_BODY_BYTES = 64 * 1024

FREE_TEXT_FIELDS = (
    "question",
    "notes",
    "establishment.name",
    "establishment.state",
    "requester.organisation",
)


@dataclass
class IngestionOutcome:
    request: ComplianceRequest | None
    field_errors: list[dict[str, Any]] = field(default_factory=list)
    guardrail: GuardrailResult | None = None
    quarantined: bool = False
    quarantine_id: str | None = None
    correlation_id: str = ""
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.request is not None and not self.quarantined


def _flatten_free_text(payload: dict[str, Any]) -> dict[str, str]:
    values: dict[str, str] = {}
    if isinstance(payload.get("question"), str):
        values["question"] = payload["question"]
    if isinstance(payload.get("notes"), str):
        values["notes"] = payload["notes"]
    est = payload.get("establishment")
    if isinstance(est, dict):
        for key in ("name", "state"):
            if isinstance(est.get(key), str):
                values[f"establishment.{key}"] = est[key]
    req = payload.get("requester")
    if isinstance(req, dict) and isinstance(req.get("organisation"), str):
        values["requester.organisation"] = req["organisation"]
    return values


def _pydantic_field_errors(exc: ValidationError) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for item in exc.errors():
        loc = ".".join(str(part) for part in item.get("loc", ()) if part != "__root__") or "request"
        msg = item.get("msg", "invalid value")
        if item.get("type") == "value_error" and item.get("ctx", {}).get("error"):
            msg = str(item["ctx"]["error"])
            # Cross-field messages name the fields. Expose a stable field list.
            fields = [f for f in ("employment.end_date", "employment.start_date", "employment.completed_service_months", "employment.expected_delivery_date", "consent") if f.split(".")[-1] in msg]
            if fields:
                for field_name in fields:
                    errors.append({"field": field_name, "code": "cross_field", "message": msg})
                continue
        errors.append({"field": loc, "code": item.get("type", "validation_error"), "message": msg})
    return errors


def validate_payload(
    payload: dict[str, Any] | str | bytes,
    *,
    correlation_id: str = "",
    injection_mode: str | None = None,
    toxicity_mode: str | None = None,
) -> IngestionOutcome:
    """The single ingestion boundary.  No agent is reachable from here."""
    correlation_id = correlation_id or new_id("corr")
    if isinstance(payload, str | bytes):
        if isinstance(payload, bytes) and len(payload) > MAX_BODY_BYTES:
            return IngestionOutcome(
                request=None,
                field_errors=[{"field": "body", "code": "too_large", "message": "body exceeds 64 KiB"}],
                quarantined=True,
                correlation_id=correlation_id,
                reason="body_too_large",
            )
        try:
            payload = json_loads(payload)
        except Exception:
            payload = None
    if not isinstance(payload, dict):
        return IngestionOutcome(
            request=None,
            field_errors=[{"field": "body", "code": "invalid_json", "message": "body must be a JSON object"}],
            quarantined=True,
            correlation_id=correlation_id,
            reason="invalid_json",
        )

    free_text = _flatten_free_text(payload)
    guardrail = check_free_text(
        free_text,
        injection_mode=injection_mode or settings.injection_mode,
        toxicity_mode=toxicity_mode or settings.toxicity_mode,
    )
    if guardrail.blocked:
        errors = [
            {
                "field": finding.match.split(":", 1)[0],
                "code": finding.kind,
                "message": f"{finding.kind} detected ({finding.pattern}, {finding.severity})",
            }
            for finding in guardrail.findings
        ]
        return IngestionOutcome(
            request=None,
            field_errors=errors,
            guardrail=guardrail,
            quarantined=True,
            correlation_id=correlation_id,
            reason="guardrail",
        )

    if guardrail.action == "sanitise":
        sanitised = sanitise_mapping(free_text)
        payload = dict(payload)
        if "question" in sanitised:
            payload["question"] = sanitised["question"]
        if "notes" in sanitised:
            payload["notes"] = sanitised["notes"]
        if isinstance(payload.get("establishment"), dict):
            payload["establishment"] = dict(payload["establishment"])
            for key, target in (("name", "establishment.name"), ("state", "establishment.state")):
                if target in sanitised:
                    payload["establishment"][key] = sanitised[target]
        if isinstance(payload.get("requester"), dict) and "requester.organisation" in sanitised:
            payload["requester"] = dict(payload["requester"])
            payload["requester"]["organisation"] = sanitised["requester.organisation"]

    try:
        request = ComplianceRequest.model_validate(payload)
    except ValidationError as exc:
        return IngestionOutcome(
            request=None,
            field_errors=_pydantic_field_errors(exc),
            guardrail=guardrail,
            quarantined=True,
            correlation_id=correlation_id,
            reason="schema",
        )

    return IngestionOutcome(
        request=request,
        guardrail=guardrail,
        quarantined=False,
        correlation_id=correlation_id,
    )
