"""Demonstrate the verifier catching an unsupported draft, then escalating.

This is a demonstration harness, not a production provider.  It replaces the
model with one deliberately bad draft: the draft is lexically close to the
retrieved second proviso to s. 53(1), but substitutes an invented 99-year
service period.  The deterministic verifier must reject it.  The graph then
performs its bounded re-search and, because the bad draft is still produced,
escalates instead of publishing the claim.

Run from the repository root:

    PYTHONPATH=src python3 scripts/demo_self_verification.py
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from compliance.agents.graph import run_advisory
from compliance.contracts import ComplianceRequest
from compliance.llm.base import LLMResult

REPO_ROOT = Path(__file__).resolve().parents[1]
BAD_CLAIM = {
    "text": (
        "The completion of continuous service of 99 years shall not be necessary where the "
        "termination of the employment is due to expiration of fixed term employment."
    ),
    "citations": ["ssc2020-s53-1-second-proviso::c1"],
    "kind": "operative",
}


class BadDraftLLM:
    """Return a plausible but numerically unsupported draft on every attempt."""

    provider = "demo_bad_draft"

    async def complete(
        self, *, task: str, prompt: str, prompt_version: str, **_kwargs
    ) -> LLMResult:
        if task == "draft":
            payload = {"claims": [BAD_CLAIM]}
        elif task == "intake":
            payload = {"domain": "gratuity"}
        else:
            payload = {}
        return LLMResult(
            text=json.dumps(payload),
            provider=self.provider,
            model="demo",
            prompt_tokens=1,
            completion_tokens=1,
        )


async def main() -> None:
    case = next(
        json.loads(line)
        for line in (REPO_ROOT / "evals" / "golden.jsonl").read_text(encoding="utf-8").splitlines()
        if json.loads(line)["id"] == "fixed_expiry_18m"
    )
    request = ComplianceRequest.model_validate(case["request"])
    trace: list[dict] = []
    result = await run_advisory(request, variant="v2", llm=BadDraftLLM(), trace_sink=trace)
    retries = [
        event
        for event in trace
        if event["kind"] == "step_start" and event["agent"] == "research_retry"
    ]
    print(
        json.dumps(
            {
                "run_id": result.run_id,
                "status": result.status.value,
                "route": result.route.value,
                "route_basis": result.route_decision.basis,
                "verification": result.verification.outcome.value,
                "loops": result.verification.loops,
                "issues": [issue.model_dump() for issue in result.verification.issues],
                "research_retry_steps": len(retries),
                "published_claims": len(result.claims),
                "published_citations": len(result.citations),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
