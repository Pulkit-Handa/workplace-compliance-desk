"""Verification tests.

Verification is the gate between a plausible draft and a published advisory.
The tests exercise each rule independently: retrieval-bound citations, the
in-force date, passage support, numeric consistency, qualifier completeness, and
the prohibition on legal determinations.
"""
from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from compliance.agents.verification import _in_force_on, _numbers, _support, verify
from compliance.contracts import Claim, ComplianceRequest
from compliance.util import match_tokens, numbers_in

REPO_ROOT = Path(__file__).resolve().parents[1]


def _request(case_id: str) -> ComplianceRequest:
    for line in (REPO_ROOT / "evals" / "golden.jsonl").read_text(encoding="utf-8").splitlines():
        case = json.loads(line)
        if case["id"] == case_id:
            return ComplianceRequest.model_validate(copy.deepcopy(case["request"]))
    raise AssertionError(f"golden case {case_id!r} not found")


def _chunk(
    provision_id: str,
    text: str,
    *,
    status: str = "in_force",
    effective_from: str = "2025-11-21",
    effective_to: str | None = None,
    citation: str = "Test provision",
) -> dict:
    return {
        "id": f"{provision_id}::c1",
        "provision_id": provision_id,
        "text": text,
        "citation": citation,
        "status": status,
        "effective_from": effective_from,
        "effective_to": effective_to,
        "repealed_by": "ssc2020" if status == "repealed" else None,
    }


def _ctx(request, as_of, claims, evidence):
    from compliance.agents.policy import classify

    def cited_provision_ids() -> set[str]:
        return {
            evidence[citation]["provision_id"]
            for claim in claims
            for citation in claim.citations
            if citation in evidence
        }

    return SimpleNamespace(
        request=request,
        as_of=as_of,
        claims=claims,
        evidence=evidence,
        loop=0,
        domain=classify(request)[0],
        cited_provision_ids=cited_provision_ids,
    )


def test_numbers_handles_digits_words_and_compounds():
    assert numbers_in("240 days") == {240}
    assert numbers_in("two hundred and forty days") == {240}
    assert numbers_in("twenty-six weeks") == {26}
    assert numbers_in("₹3,500") == {3500}
    assert numbers_in("five years and 15 days") == {5, 15}


def test_section_references_are_not_treated_as_quantities():
    assert _numbers("s. 60(3) requires 240 days") == {240}
    assert _numbers("s. 53(6) and s. 164(1)") == set()


def test_support_uses_numbers_and_stems():
    passage = "The employee must have worked for not less than 240 days in twelve months."
    assert _support("An employee worked 240 days in twelve months", passage) > 0.9
    assert _support("maternity crèche facility", passage) == 0.0


def test_match_tokens_canonicalise_number_forms():
    assert match_tokens("two hundred and forty days") == match_tokens("240 days")


def test_in_force_boundaries_include_the_last_day():
    chunk = {"effective_from": "1972-09-16", "effective_to": "2025-11-20"}
    assert _in_force_on(chunk, date(2025, 11, 20)) is True
    assert _in_force_on(chunk, date(2025, 11, 21)) is False
    assert _in_force_on(chunk, date(1972, 9, 15)) is False
    assert _in_force_on({"effective_from": "2025-11-21", "effective_to": None}, date(2026, 10, 26)) is True


def test_historical_1972_claim_verifies_cleanly():
    request = _request("historical")
    evidence = {
        "gratuity1972-s4::c1": _chunk(
            "gratuity1972-s4",
            "Gratuity shall be payable to an employee on termination of employment after continuous "
            "service of not less than five years, on superannuation, retirement or resignation.",
            status="repealed",
            effective_from="1972-09-16",
            effective_to="2025-11-20",
            citation="Payment of Gratuity Act, 1972, s. 4",
        )
    }
    claims = [
        Claim(
            id="clm_01",
            text="Gratuity is payable on resignation after continuous service of not less than five years.",
            citations=["gratuity1972-s4::c1"],
            kind="operative",
        )
    ]
    result = verify(_ctx(request, date(2024, 6, 1), claims, evidence))
    assert result.outcome.value == "passed"
    assert result.claims_checked == 1
    assert result.claims_supported == 1
    assert result.issues == []
    assert claims[0].supported is True


def test_current_claim_cannot_rely_on_repealed_act():
    request = _request("resignation_7y")
    evidence = {
        "gratuity1972-s4::c1": _chunk(
            "gratuity1972-s4",
            "Gratuity is payable after continuous service of not less than five years.",
            status="repealed",
            effective_from="1972-09-16",
            effective_to="2025-11-20",
        )
    }
    claims = [
        Claim(
            id="clm_01",
            text="Gratuity is payable after continuous service of not less than five years.",
            citations=["gratuity1972-s4::c1"],
        )
    ]
    result = verify(_ctx(request, date(2026, 1, 1), claims, evidence))
    reasons = {issue.reason for issue in result.issues}
    assert "not_in_force_on_date" in reasons
    assert result.outcome.value == "failed"


def test_claim_without_citation_fails():
    request = _request("resignation_7y")
    claims = [Claim(id="clm_01", text="Gratuity is payable after seven years.", citations=[])]
    result = verify(_ctx(request, date(2026, 1, 1), claims, {}))
    assert "no_citation" in {issue.reason for issue in result.issues}


def test_citation_not_retrieved_in_this_run_fails():
    request = _request("resignation_7y")
    claims = [
        Claim(
            id="clm_01",
            text="Gratuity is payable after continuous service of five years.",
            citations=["ssc2020-s53-1::c9"],
        )
    ]
    result = verify(_ctx(request, date(2026, 1, 1), claims, {}))
    assert "citation_not_retrieved" in {issue.reason for issue in result.issues}


def test_weak_passage_support_fails():
    request = _request("resignation_7y")
    evidence = {
        "ssc2020-s53-1::c1": _chunk(
            "ssc2020-s53-1",
            "Gratuity is payable to an employee after continuous service of not less than five years.",
        )
    }
    claims = [
        Claim(
            id="clm_01",
            text="The employer must provide a crèche and transport allowance to every contractor.",
            citations=["ssc2020-s53-1::c1"],
        )
    ]
    result = verify(_ctx(request, date(2026, 1, 1), claims, evidence))
    assert "weak_passage_support" in {issue.reason for issue in result.issues}


def test_unsupported_number_fails():
    request = _request("resignation_7y")
    evidence = {
        "ssc2020-s53-1::c1": _chunk(
            "ssc2020-s53-1",
            "Gratuity is payable to an employee on termination of employment after continuous service "
            "of not less than five years on superannuation, retirement or resignation.",
        )
    }
    claims = [
        Claim(
            id="clm_01",
            text="Gratuity is payable to an employee on termination after continuous service of not less "
            "than nine years on superannuation, retirement or resignation (s. 53(1)).",
            citations=["ssc2020-s53-1::c1"],
        )
    ]
    result = verify(_ctx(request, date(2026, 1, 1), claims, evidence))
    # The five-year provision is also a missing qualifier for gratuity, but the
    # decisive numeric failure must be present.
    assert "number_not_supported" in {issue.reason for issue in result.issues}


def test_missing_qualifier_fails_even_when_main_claim_is_supported():
    request = _request("death")
    evidence = {
        "ssc2020-s53-1::c1": _chunk(
            "ssc2020-s53-1",
            "Gratuity is payable to an employee on termination of employment after continuous service "
            "of not less than five years on superannuation, retirement or resignation, death or disablement.",
        )
    }
    claims = [
        Claim(
            id="clm_01",
            text="Gratuity is payable on termination of employment after continuous service of not less "
            "than five years (s. 53(1)).",
            citations=["ssc2020-s53-1::c1"],
        )
    ]
    result = verify(_ctx(request, date(2026, 1, 1), claims, evidence))
    reasons = {issue.reason for issue in result.issues}
    assert "qualifier_missing" in reasons
    missing = [issue for issue in result.issues if issue.reason == "qualifier_missing"]
    assert any("second proviso" in issue.detail for issue in missing)


def test_conflicting_repealed_and_in_force_citations_fail():
    request = _request("resignation_7y")
    evidence = {
        "ssc2020-s53-1::c1": _chunk(
            "ssc2020-s53-1",
            "Gratuity is payable after continuous service of not less than five years.",
        ),
        "gratuity1972-s4::c1": _chunk(
            "gratuity1972-s4",
            "Gratuity is payable after continuous service of not less than five years.",
            status="repealed",
            effective_from="1972-09-16",
            effective_to="2025-11-20",
        ),
    }
    claims = [
        Claim(
            id="clm_01",
            text="Gratuity is payable after continuous service of not less than five years.",
            citations=["ssc2020-s53-1::c1", "gratuity1972-s4::c1"],
        )
    ]
    result = verify(_ctx(request, date(2026, 1, 1), claims, evidence))
    assert "conflicting_status" in {issue.reason for issue in result.issues}


def test_escalation_claim_is_supported_without_citation():
    request = _request("outside")
    claims = [
        Claim(
            id="clm_01",
            text="The desk cannot answer this request from the indexed corpus.",
            citations=[],
            kind="escalation",
        )
    ]
    result = verify(_ctx(request, date(2026, 1, 1), claims, {}))
    assert result.outcome.value == "passed"
    assert claims[0].supported is True


def test_unauthorised_determination_is_flagged():
    request = _request("resignation_7y")
    evidence = {
        "ssc2020-s53-1::c1": _chunk(
            "ssc2020-s53-1",
            "Gratuity is payable to an employee on termination of employment after continuous service "
            "of not less than five years on superannuation, retirement or resignation.",
        )
    }
    claims = [
        Claim(
            id="clm_01",
            text="The employee is guilty of misconduct and gratuity is payable after five years.",
            citations=["ssc2020-s53-1::c1"],
        )
    ]
    result = verify(_ctx(request, date(2026, 1, 1), claims, evidence))
    assert "unauthorised_determination" in {issue.reason for issue in result.issues}


def test_verification_issue_deduplication_preserves_order():
    request = _request("resignation_7y")
    claims = [
        Claim(id="clm_01", text="An unsupported statement about gratuity.", citations=[]),
        Claim(id="clm_02", text="Another unsupported statement about gratuity.", citations=[]),
    ]
    result = verify(_ctx(request, date(2026, 1, 1), claims, {}))
    keys = [(issue.claim_id, issue.reason, issue.detail) for issue in result.issues]
    assert len(keys) == len(set(keys))
