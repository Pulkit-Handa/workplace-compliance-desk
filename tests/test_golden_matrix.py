"""Golden-set matrix: the published behaviour on the synthetic reference cases.

This is the regression gate for the product, not a unit test of one helper.  Each
case must reach the expected route, pass or escalate verification as expected,
and cite every decisive provision (including qualifiers).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from compliance.agents.graph import run_advisory
from compliance.contracts import ComplianceRequest
from compliance.llm.base import LLMResult

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLDEN = REPO_ROOT / "evals" / "golden.jsonl"


def _cases() -> list[dict]:
    return [json.loads(line) for line in GOLDEN.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.parametrize("case", _cases(), ids=[case["id"] for case in _cases()])
@pytest.mark.asyncio
async def test_golden_case(case):
    request = ComplianceRequest.model_validate(case["request"])
    result = await run_advisory(request, variant="v2")
    cited = {citation.id.split("::", 1)[0] for citation in result.citations}

    assert result.route.value == case["expected_route"], (
        f"{case['id']}: route {result.route.value!r}, basis={result.route_decision.basis!r}, "
        f"issues={[issue.reason for issue in result.verification.issues]}"
    )
    assert result.verification.outcome.value == case["expected_verification"], (
        f"{case['id']}: verification issues={[issue.model_dump() for issue in result.verification.issues]}"
    )
    required = set(case.get("required_provisions") or [])
    qualifiers = set(case.get("qualifier_provisions") or [])
    assert required <= cited, f"{case['id']}: missing required citations {sorted(required - cited)}"
    assert qualifiers <= cited, f"{case['id']}: missing qualifiers {sorted(qualifiers - cited)}"
    assert result.advisory
    assert "not legal advice" in result.advisory
    assert result.disclaimer in result.advisory
    assert result.index_version
    assert result.prompt_versions.get("draft") == "draft-v2"
    assert result.verification.claims_checked == len(result.claims)


class _UnsupportedDraftLLM:
    provider = "test_bad_draft"

    async def complete(
        self, *, task: str, prompt: str, prompt_version: str, **_kwargs
    ) -> LLMResult:
        if task == "draft":
            payload = {
                "claims": [
                    {
                        "text": (
                            "The completion of continuous service of 99 years shall not be "
                            "necessary where the termination of the employment is due to "
                            "expiration of fixed term employment."
                        ),
                        "citations": ["ssc2020-s53-1-second-proviso::c1"],
                        "kind": "operative",
                    }
                ]
            }
        else:
            payload = {}
        return LLMResult(text=json.dumps(payload), provider=self.provider, prompt_tokens=1, completion_tokens=1)


@pytest.mark.asyncio
async def test_failed_draft_is_withheld_and_escalated():
    case = next(item for item in _cases() if item["id"] == "fixed_expiry_18m")
    draft: dict = {}
    result = await run_advisory(
        ComplianceRequest.model_validate(case["request"]),
        variant="v2",
        llm=_UnsupportedDraftLLM(),
        draft_sink=draft,
    )
    assert result.route.value == "legal_counsel_review"
    assert result.verification.outcome.value == "failed"
    assert result.verification.loops == 2
    assert "number_not_supported" in {issue.reason for issue in result.verification.issues}
    assert result.claims == []
    assert result.citations == []
    # The evaluator can still inspect the withheld draft without exposing it.
    assert len(draft["claims"]) == 1
    assert "99 years" in draft["claims"][0].text
    assert draft["citations"]
    assert "99 years" not in result.advisory
    assert "Verification did not pass" in result.summary


@pytest.mark.asyncio
async def test_below_threshold_case_does_not_show_a_gratuity_calculation():
    case = next(item for item in _cases() if item["id"] == "resignation_3y")
    result = await run_advisory(ComplianceRequest.model_validate(case["request"]), variant="v2")
    assert result.route.value == "answer_with_citation"
    assert "not payable" in result.summary.lower()
    assert "Indicative arithmetic" not in result.advisory


@pytest.mark.asyncio
async def test_required_multichunk_provision_cites_the_operative_chunk():
    # ssc2020-s67 has an operative crèche chunk (c1) and an intimation chunk (c2).
    # Retrieval may surface c2 first; the required-provision fetch must make c1
    # available so the claim is cited against the passage that supports it.
    case = next(item for item in _cases() if item["id"] == "maternity_creche_50")
    result = await run_advisory(ComplianceRequest.model_validate(case["request"]), variant="v2")
    citation_ids = {citation.id for citation in result.citations}
    assert "ssc2020-s67::c1" in citation_ids, sorted(citation_ids)
    assert result.verification.outcome.value == "passed"

@pytest.mark.asyncio
async def test_out_of_scope_case_escalates_without_borrowing_a_citation():
    case = next(item for item in _cases() if item["id"] == "outside")
    result = await run_advisory(ComplianceRequest.model_validate(case["request"]), variant="v2")
    assert result.route.value == "legal_counsel_review"
    assert result.domain.value == "other"
    assert result.citations == []
    assert result.verification.outcome.value == "passed"
    assert any(claim.kind == "escalation" for claim in result.claims)


@pytest.mark.asyncio
async def test_historical_case_uses_the_1972_act_as_operative_authority():
    case = next(item for item in _cases() if item["id"] == "historical")
    result = await run_advisory(ComplianceRequest.model_validate(case["request"]), variant="v2")
    assert result.route.value == "answer_with_citation"
    assert any(
        citation.statute.startswith("Payment of Gratuity Act")
        and citation.status == "repealed"
        for citation in result.citations
    )


@pytest.mark.asyncio
async def test_current_fixed_term_case_keeps_the_repealed_act_as_background_only():
    case = next(item for item in _cases() if item["id"] == "fixed_expiry")
    result = await run_advisory(ComplianceRequest.model_validate(case["request"]), variant="v2")
    operative = [claim for claim in result.claims if claim.kind == "operative"]
    for claim in operative:
        for citation_id in claim.citations:
            citation = next(item for item in result.citations if item.id == citation_id)
            assert citation.status == "in_force"
    assert "repealed" in result.summary.lower() or any(
        "repealed" in claim.text.lower() for claim in result.claims
    )


@pytest.mark.asyncio
async def test_complaint_case_escalates_but_still_cites_the_procedure():
    case = next(item for item in _cases() if item["id"] == "posh_complaint")
    result = await run_advisory(ComplianceRequest.model_validate(case["request"]), variant="v2")
    assert result.route.value == "legal_counsel_review"
    assert result.verification.outcome.value == "passed"
    cited = {citation.id.split("::", 1)[0] for citation in result.citations}
    assert {"posh2013-s4", "posh2013-s9"} <= cited
    assert any(claim.kind == "escalation" for claim in result.claims)
