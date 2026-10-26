"""Published request-contract tests.

The contract is the first line of defence: a payload that does not satisfy it
never reaches the queue or an agent.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from compliance.contracts import (
    ComplianceRequest,
    Employment,
    EmploymentType,
    Establishment,
    Requester,
    SeparationEvent,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _golden_payload(case_id: str = "fixed_expiry") -> dict:
    for line in (REPO_ROOT / "evals" / "golden.jsonl").read_text(encoding="utf-8").splitlines():
        case = json.loads(line)
        if case["id"] == case_id:
            return copy.deepcopy(case["request"])
    raise AssertionError(f"golden case {case_id!r} not found")


def test_golden_request_is_valid():
    request = ComplianceRequest.model_validate(_golden_payload())
    assert request.consent is True
    assert request.employment.worker_type is EmploymentType.FIXED_TERM
    assert request.employment.event is SeparationEvent.EXPIRY


def test_consent_must_be_attested():
    payload = _golden_payload()
    payload["consent"] = False
    with pytest.raises(ValidationError) as exc:
        ComplianceRequest.model_validate(payload)
    assert "consent must be true" in str(exc.value)


def test_unknown_fields_are_rejected():
    payload = _golden_payload()
    payload["surprise"] = "not in the contract"
    with pytest.raises(ValidationError) as exc:
        ComplianceRequest.model_validate(payload)
    assert "surprise" in str(exc.value)


def test_question_length_is_bounded():
    payload = _golden_payload()
    payload["question"] = "too short"
    with pytest.raises(ValidationError):
        ComplianceRequest.model_validate(payload)


def test_client_id_is_constrained():
    with pytest.raises(ValidationError):
        Requester(client_id="has spaces!")
    assert Requester(client_id="matrix:eu-1").client_id == "matrix:eu-1"


def test_end_date_must_follow_start_date():
    payload = _golden_payload()
    payload["employment"]["start_date"] = "2025-01-01"
    payload["employment"]["end_date"] = "2024-01-01"
    payload["employment"]["completed_service_months"] = 12
    with pytest.raises(ValidationError) as exc:
        ComplianceRequest.model_validate(payload)
    assert "end_date must be after" in str(exc.value)


def test_future_employment_dates_are_rejected():
    payload = _golden_payload()
    payload["as_of_date"] = "2024-01-01"
    with pytest.raises(ValidationError) as exc:
        ComplianceRequest.model_validate(payload)
    assert "cannot be after as_of_date" in str(exc.value)


def test_expiry_requires_fixed_term_or_contract():
    payload = _golden_payload()
    payload["employment"]["worker_type"] = "permanent"
    with pytest.raises(ValidationError) as exc:
        ComplianceRequest.model_validate(payload)
    assert "requires worker_type fixed_term or contract" in str(exc.value)


def test_fixed_term_end_date_requires_start_date():
    payload = _golden_payload()
    payload["employment"].pop("start_date")
    payload["employment"]["completed_service_months"] = 36
    with pytest.raises(ValidationError) as exc:
        ComplianceRequest.model_validate(payload)
    assert "also requires start_date" in str(exc.value)


def test_completed_service_must_match_dates():
    payload = _golden_payload()
    payload["employment"]["completed_service_months"] = 12
    with pytest.raises(ValidationError) as exc:
        ComplianceRequest.model_validate(payload)
    assert "inconsistent with start_date and end_date" in str(exc.value)


def test_expected_delivery_date_cannot_precede_false_pregnancy():
    payload = _golden_payload("maternity")
    payload["employment"]["is_pregnant"] = False
    payload["employment"]["expected_delivery_date"] = "2026-01-01"
    with pytest.raises(ValidationError) as exc:
        ComplianceRequest.model_validate(payload)
    assert "expected_delivery_date cannot be supplied" in str(exc.value)


def test_employee_count_is_bounded():
    with pytest.raises(ValidationError):
        Establishment(employee_count=0)
    with pytest.raises(ValidationError):
        Establishment(employee_count=1_000_001)


def test_employment_enum_parsing():
    employment = Employment(worker_type="fixed_term", event="expiry")
    assert employment.worker_type is EmploymentType.FIXED_TERM
    assert employment.event is SeparationEvent.EXPIRY
