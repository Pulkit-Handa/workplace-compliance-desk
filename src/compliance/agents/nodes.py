from __future__ import annotations

import json
from typing import Any

from compliance.agents import policy
from compliance.agents.state import RunContext
from compliance.agents.verification import _support, verify
from compliance.config import settings
from compliance.contracts import Claim, Domain, Route, VerificationOutcome
from compliance.observability import metrics
from compliance.tools import ToolBudgetExceeded
from compliance.util import json_loads, normalise_text, truncate


def _evidence_prompt(ctx: RunContext, limit: int = 14, width: int = 900) -> str:
    items = sorted(ctx.evidence_list(), key=lambda item: item.get("evidence_score", 0.0), reverse=True)[:limit]
    lines: list[str] = []
    for item in items:
        lines.append(
            f"[{item['id']}] {item['citation']} | {item['status'].upper()} | "
            f"{item.get('effective_from') or '?'} to {item.get('effective_to') or 'present'}\n"
            f"{truncate(item.get('text', ''), width)}"
        )
    return "\n\n".join(lines)


def _safe_json(text: str) -> dict[str, Any]:
    value = json_loads(text, default=None)
    if isinstance(value, dict):
        return value
    if isinstance(text, str):
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            value = json_loads(text[start : end + 1], default=None)
            if isinstance(value, dict):
                return value
    return {}


def _normalise_citations(ctx: RunContext, citations: Any) -> list[str]:
    if not isinstance(citations, list):
        return []
    result: list[str] = []
    for citation in citations:
        if not isinstance(citation, str):
            continue
        if citation in ctx.evidence:
            result.append(citation)
            continue
        chunk = ctx.best_for(citation)
        if chunk is not None:
            result.append(chunk["id"])
    return list(dict.fromkeys(result))


def _claims_from_llm(ctx: RunContext, text: str) -> list[Claim] | None:
    """Accept a model draft only if every claim is citation-bound and supported.

    The model is never allowed to create a claim without a retrieved passage.
    Anything that does not clear this bar is discarded and the deterministic
    policy draft is used instead.
    """
    payload = _safe_json(text)
    raw_claims = payload.get("claims")
    if not isinstance(raw_claims, list) or not raw_claims:
        return None
    candidates: list[Claim] = []
    for index, raw in enumerate(raw_claims[:10], start=1):
        if not isinstance(raw, dict):
            return None
        claim_text = raw.get("text")
        if not isinstance(claim_text, str) or not (10 <= len(claim_text) <= 700):
            return None
        citations = _normalise_citations(ctx, raw.get("citations"))
        if not citations:
            return None
        kind = raw.get("kind")
        if kind not in {"operative", "background", "procedural", "escalation"}:
            kind = "operative"
        combined = " ".join(ctx.evidence[cid].get("text", "") for cid in citations)
        if kind != "escalation" and _support(claim_text, combined) < 0.5:
            return None
        candidates.append(
            Claim(
                id=f"clm_{index:02d}",
                text=normalise_text(claim_text),
                citations=citations,
                kind=kind,  # type: ignore[arg-type]
            )
        )
    return candidates


async def intake_node(state: dict[str, Any]) -> dict[str, Any]:
    ctx: RunContext = state["ctx"]
    started = ctx.start_step("intake")
    domain, risks = policy.classify(ctx.request)
    ctx.domain = domain
    ctx.risks = risks

    prompt = ctx.prompts.render(
        "intake",
        ctx.variant,
        question=truncate(ctx.request.question, 1800),
    )
    result = await ctx.complete(step="intake", task="intake", prompt=prompt.text, max_tokens=300)
    model_domain = None
    if result is not None:
        payload = _safe_json(result.text)
        if payload.get("domain") in {item.value for item in Domain}:
            model_domain = Domain(payload["domain"])
            if model_domain != domain:
                ctx.event(
                    "agent_note",
                    agent="intake",
                    name="model_disagrees_with_policy",
                    data={"model": model_domain.value, "policy": domain.value},
                )
    coverage_result = await ctx.toolbox.coverage(
        domain=domain.value,
        establishment_type=ctx.request.establishment.establishment_type.value,
        employee_count=ctx.request.establishment.employee_count,
    )
    if coverage_result.ok and coverage_result.data is not None:
        ctx.coverage = coverage_result.data
    else:
        ctx.errors.append(f"coverage_tool_failed:{coverage_result.error}")
        ctx.degraded = True
    ctx.event(
        "tool_call",
        agent="intake",
        name="check_coverage",
        data={**coverage_result.as_trace(), "coverage": ctx.coverage.as_dict() if ctx.coverage else None},
    )
    ctx.end_step(
        "intake",
        started,
        domain=domain.value,
        risks=risks,
        coverage=ctx.coverage.as_dict() if ctx.coverage else None,
    )
    return {"ctx": ctx}


async def research_node(state: dict[str, Any]) -> dict[str, Any]:
    ctx: RunContext = state["ctx"]
    started = ctx.start_step("research")
    plan = policy.query_plan(ctx)
    await _execute_plan(ctx, plan)
    if ctx.variant == "v2":
        await _fetch_required(ctx, policy.required_provisions(ctx))
        await _expand_related(ctx)
    ctx.end_step(
        "research",
        started,
        queries=len(plan),
        evidence=len(ctx.evidence_order),
        variant=ctx.variant,
    )
    return {"ctx": ctx}


async def research_retry_node(state: dict[str, Any]) -> dict[str, Any]:
    ctx: RunContext = state["ctx"]
    started = ctx.start_step("research_retry")
    ctx.loop += 1
    retry_reason = (
        ctx.verification.issues[0].reason
        if ctx.verification is not None and ctx.verification.issues
        else "verification_failure"
    )
    metrics.RETRIES_TOTAL.labels(reason=retry_reason).inc()
    suggestions = []
    if ctx.verification is not None:
        suggestions = [
            issue.suggested_query
            for issue in ctx.verification.issues
            if issue.suggested_query
        ]
    plan = policy.query_plan(ctx, missing=list(dict.fromkeys(suggestions)))
    if not plan:
        plan = policy.query_plan(ctx)
    await _execute_plan(ctx, plan)
    missing_required = [pid for pid in policy.required_provisions(ctx) if not ctx.has_provision(pid)]
    if missing_required:
        await _fetch_required(ctx, missing_required)
    ctx.end_step(
        "research_retry",
        started,
        loop=ctx.loop,
        queries=len(plan),
        evidence=len(ctx.evidence_order),
    )
    return {"ctx": ctx}


async def _execute_plan(ctx: RunContext, plan: list[policy.QuerySpec]) -> None:
    for spec in plan:
        if ctx.remaining_s() <= 0.4:
            ctx.errors.append("wall_clock_before_search")
            ctx.degraded = True
            break
        try:
            result = await ctx.toolbox.search(
                spec.query,
                top_k=spec.top_k,
                domain=ctx.domain.value if ctx.domain is not Domain.OTHER else None,
            )
        except ToolBudgetExceeded as exc:
            ctx.errors.append(f"tool_budget:{exc}")
            ctx.degraded = True
            break
        ctx.evidence_queries.append(
            {"query": spec.query, "reason": spec.reason, "hits": len(result.data or [])}
        )
        ctx.event(
            "tool_call",
            agent="research",
            name="search_provisions",
            data={**result.as_trace(), "query": spec.query, "reason": spec.reason},
        )
        if not result.ok or not result.data:
            continue
        for hit in result.data:
            chunk = hit.chunk.as_dict()
            ctx.add_evidence(
                chunk,
                reason=f"search:{spec.reason}",
                score=float(hit.score) + max(0.0, 0.1 - hit.rank * 0.01),
            )


async def _fetch_required(ctx: RunContext, provision_ids: list[str]) -> None:
    # A provision may be split across several chunks.  Seeing one chunk is not
    # enough: the proposition may live in a later chunk (for example the crèche
    # duty in s. 67(1) versus the intimation duty in s. 67(2)).  Fetch the whole
    # provision when any of its chunks is missing.
    missing = [
        pid
        for pid in provision_ids
        if any(
            chunk.id not in ctx.evidence
            for chunk in ctx.toolbox.store.get_provision_chunks(pid)
        )
    ]
    if not missing:
        return
    try:
        result = await ctx.toolbox.related(missing)
    except ToolBudgetExceeded as exc:
        ctx.errors.append(f"tool_budget:{exc}")
        ctx.degraded = True
        return
    ctx.event(
        "tool_call",
        agent="research",
        name="related_provisions",
        data={**result.as_trace(), "provision_ids": missing},
    )
    for chunk in result.data or []:
        ctx.add_evidence(chunk.as_dict(), reason="curated:required", score=0.55)


async def _expand_related(ctx: RunContext) -> None:
    top = sorted(
        ctx.evidence_list(), key=lambda item: item.get("evidence_score", 0.0), reverse=True
    )[:5]
    provision_ids = list(dict.fromkeys(item["provision_id"] for item in top))
    if not provision_ids:
        return
    try:
        result = await ctx.toolbox.related(provision_ids)
    except ToolBudgetExceeded as exc:
        ctx.errors.append(f"tool_budget:{exc}")
        ctx.degraded = True
        return
    ctx.event(
        "tool_call",
        agent="research",
        name="related_provisions",
        data={**result.as_trace(), "provision_ids": provision_ids, "mode": "expand"},
    )
    for chunk in result.data or []:
        ctx.add_evidence(chunk.as_dict(), reason="related", score=0.35)


async def draft_node(state: dict[str, Any]) -> dict[str, Any]:
    ctx: RunContext = state["ctx"]
    started = ctx.start_step("draft")
    evidence = _evidence_prompt(ctx)
    prompt = ctx.prompts.render(
        "draft",
        ctx.variant,
        question=truncate(ctx.request.question, 1600),
        domain=ctx.domain.value,
        evidence=evidence,
    )
    result = await ctx.complete(step="draft", task="draft", prompt=prompt.text, max_tokens=900)

    # Re-drafting after a verification loop must replace, not append to, the
    # previous set of claims.
    ctx.claims = []
    ctx.citations = {}

    # Deterministic policy draft first: it is the safe floor.
    policy.compose(ctx)
    accepted = False
    if result is not None:
        model_claims = _claims_from_llm(ctx, result.text)
        if model_claims:
            ctx.claims = model_claims
            # Drop citations that the accepted draft does not use.
            used = {cid for claim in model_claims for cid in claim.citations}
            ctx.citations = {cid: citation for cid, citation in ctx.citations.items() if cid in used}
            accepted = True
    if not ctx.claims:
        policy._add_claim(
            ctx,
            "No retrieved passage supports an operative statement for this request, so the desk cannot "
            "produce a cited answer.",
            [],
            kind="escalation",
        )
        ctx.summary = (
            "No retrieved passage supports an operative statement for this request. The desk escalates rather "
            "than answering without a citation."
        )
    ctx.event(
        "agent_note",
        agent="draft",
        name="draft_selected",
        data={
            "source": "model" if accepted else "deterministic_policy",
            "claims": len(ctx.claims),
            "citations": len(ctx.citations),
            "llm_text_chars": len(result.text) if result else 0,
        },
    )
    ctx.end_step(
        "draft",
        started,
        claims=len(ctx.claims),
        citations=len(ctx.citations),
        model_draft_accepted=accepted,
    )
    return {"ctx": ctx}


async def verify_node(state: dict[str, Any]) -> dict[str, Any]:
    ctx: RunContext = state["ctx"]
    started = ctx.start_step("verify")
    claims_payload = [
        {"claim_id": claim.id, "text": claim.text, "citations": claim.citations, "kind": claim.kind}
        for claim in ctx.claims
    ]
    prompt = ctx.prompts.render(
        "verify",
        ctx.variant,
        claims=json.dumps(claims_payload, ensure_ascii=False, indent=2)[:6000],
        evidence=_evidence_prompt(ctx, limit=12, width=700),
    )
    result = await ctx.complete(step="verify", task="verify", prompt=prompt.text, max_tokens=600)
    model_issue_count = 0
    if result is not None:
        payload = _safe_json(result.text)
        raw_issues = payload.get("issues")
        if isinstance(raw_issues, list):
            model_issue_count = len(raw_issues)
    ctx.verification = verify(ctx)
    if ctx.verification.outcome is VerificationOutcome.FAILED:
        first = ctx.verification.issues[0]
        ctx.summary = (
            f"Verification did not pass ({first.reason}). The draft is withheld and the request is escalated "
            "rather than published with an unsupported or incomplete proposition."
        )
    ctx.event(
        "verification",
        agent="verify",
        name=ctx.verification.outcome.value,
        data={
            "claims_checked": ctx.verification.claims_checked,
            "claims_supported": ctx.verification.claims_supported,
            "issues": [issue.model_dump() for issue in ctx.verification.issues],
            "model_issue_count": model_issue_count,
            "loops": ctx.loop,
        },
    )
    ctx.end_step(
        "verify",
        started,
        outcome=ctx.verification.outcome.value,
        issues=len(ctx.verification.issues),
    )
    return {"ctx": ctx}


async def route_node(state: dict[str, Any]) -> dict[str, Any]:
    ctx: RunContext = state["ctx"]
    started = ctx.start_step("route")
    context = json.dumps(
        {
            "domain": ctx.domain.value,
            "risks": ctx.risks,
            "coverage": ctx.coverage.as_dict() if ctx.coverage else None,
            "verification": ctx.verification.model_dump() if ctx.verification else None,
            "issue_count": len(ctx.verification.issues) if ctx.verification else 0,
        },
        ensure_ascii=False,
        default=str,
    )
    prompt = ctx.prompts.render("route", ctx.variant, context=context[:5000])
    result = await ctx.complete(step="route", task="route", prompt=prompt.text, max_tokens=300)
    ctx.route_decision = policy.decide_route(ctx)
    if ctx.verification and ctx.verification.outcome is VerificationOutcome.FAILED:
        # A failed draft is evidence for the escalation, not an advisory.  Do
        # not publish its claims or citations to the caller or the UI.  Keep a
        # private copy so the evaluator can diagnose the failed draft.
        ctx.withheld_claims = list(ctx.claims)
        ctx.withheld_citations = dict(ctx.citations)
        ctx.claims = []
        ctx.citations = {}
    if result is not None:
        payload = _safe_json(result.text)
        model_route = payload.get("route")
        if model_route in {item.value for item in Route} and model_route != ctx.route_decision.route.value:
            ctx.event(
                "agent_note",
                agent="route",
                name="model_disagrees_with_policy",
                data={"model": model_route, "policy": ctx.route_decision.route.value},
            )
    ctx.end_step(
        "route",
        started,
        route=ctx.route_decision.route.value,
        basis=ctx.route_decision.basis,
    )
    return {"ctx": ctx}


def after_verify(state: dict[str, Any]) -> str:
    ctx: RunContext = state["ctx"]
    if (
        ctx.variant == "v2"
        and ctx.verification is not None
        and ctx.verification.outcome is VerificationOutcome.FAILED
        and ctx.loop < settings.max_verify_loops
    ):
        return "retry"
    return "route"


__all__ = [
    "after_verify",
    "draft_node",
    "intake_node",
    "research_node",
    "research_retry_node",
    "route_node",
    "verify_node",
]
