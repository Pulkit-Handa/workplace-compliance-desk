from __future__ import annotations

import asyncio
import time
from datetime import date
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from compliance.agents import nodes
from compliance.agents.state import RunBudgetExceeded, RunContext
from compliance.config import settings
from compliance.contracts import (
    AdvisoryResult,
    ComplianceRequest,
    GuardrailReport,
    Route,
    RouteDecision,
    RunStatus,
    VerificationIssue,
    VerificationOutcome,
    VerificationResult,
)
from compliance.corpus.index import IndexStore, get_default_index
from compliance.llm.base import LLMClient
from compliance.llm.factory import get_llm_client
from compliance.llm.prompts import PromptRegistry
from compliance.logging_setup import get_logger, log_context
from compliance.observability.tracing import span
from compliance.tools import ToolBox
from compliance.util import new_id

logger = get_logger(__name__)


class GraphState(TypedDict):
    ctx: RunContext


_compiled = None


def build_graph():
    global _compiled
    if _compiled is not None:
        return _compiled
    graph = StateGraph(GraphState)
    graph.add_node("intake", nodes.intake_node)
    graph.add_node("research", nodes.research_node)
    graph.add_node("draft", nodes.draft_node)
    graph.add_node("verify", nodes.verify_node)
    graph.add_node("research_retry", nodes.research_retry_node)
    graph.add_node("route", nodes.route_node)
    graph.add_edge(START, "intake")
    graph.add_edge("intake", "research")
    graph.add_edge("research", "draft")
    graph.add_edge("draft", "verify")
    graph.add_conditional_edges(
        "verify",
        nodes.after_verify,
        {"retry": "research_retry", "route": "route"},
    )
    graph.add_edge("research_retry", "draft")
    graph.add_edge("route", END)
    _compiled = graph.compile()
    return _compiled


def reset_graph() -> None:
    global _compiled
    _compiled = None


async def run_advisory(
    request: ComplianceRequest,
    *,
    run_id: str | None = None,
    correlation_id: str | None = None,
    variant: str | None = None,
    as_of: date | None = None,
    index: IndexStore | None = None,
    llm: LLMClient | None = None,
    prompts: PromptRegistry | None = None,
    guardrails: GuardrailReport | None = None,
    trace_sink: list[dict[str, Any]] | None = None,
    evidence_sink: list[dict[str, Any]] | None = None,
    draft_sink: dict[str, Any] | None = None,
) -> AdvisoryResult:
    """Execute one advisory run through the LangGraph state machine."""
    run_id = run_id or new_id("run")
    correlation_id = correlation_id or new_id("corr")
    variant = variant or settings.prompt_variant
    index = index or get_default_index()
    prompts = prompts or PromptRegistry(variant=variant)
    llm = llm or get_llm_client()
    as_of = as_of or request.as_of_date

    ctx = RunContext(
        request=request,
        run_id=run_id,
        correlation_id=correlation_id,
        variant=variant,
        as_of=as_of,
        toolbox=ToolBox(index=index, as_of=as_of),
        llm=llm,
        prompts=prompts,
        index_version=index.index_version,
        guardrail_report=guardrails or GuardrailReport(),
    )
    ctx.event(
        "run_start",
        data={
            "variant": variant,
            "as_of": as_of.isoformat(),
            "index_version": ctx.index_version,
            "prompt_versions": prompts.versions(variant),
            "domain_hint": None,
            "client": request.requester.client_id,
        },
    )
    started = time.perf_counter()
    with log_context(correlation_id=correlation_id, run_id=run_id):
        try:
            with span(
                "advisory_run",
                {
                    "compliance.run_id": run_id,
                    "compliance.variant": variant,
                    "compliance.domain": None,
                    "compliance.as_of": as_of.isoformat(),
                },
            ):
                await build_graph().ainvoke({"ctx": ctx})
        except RunBudgetExceeded as exc:
            ctx.degraded = True
            ctx.errors.append(str(exc))
            ctx.verification = VerificationResult(
                outcome=VerificationOutcome.FAILED,
                claims_checked=len(ctx.claims),
                claims_supported=0,
                issues=[
                    VerificationIssue(
                        claim_id="run",
                        reason="budget_exceeded",
                        detail=str(exc),
                        suggested_query=None,
                    )
                ],
                loops=ctx.loop,
            )
            ctx.summary = "The run exceeded its budget before verification completed, so it is escalated."
            ctx.route_decision = RouteDecision(
                route=Route.LEGAL_COUNSEL_REVIEW,
                basis=f"Run budget exceeded: {exc}",
                confidence=0.2,
                escalation_path=["compliance_desk", "employment_counsel"],
                next_actions=["Inspect the run trace; retry with a larger budget or narrower question."],
            )
            ctx.event("run_budget_exceeded", data={"error": str(exc), "steps": ctx.steps})

        if ctx.verification is None or ctx.route_decision is None:
            # Defensive: a node failed in a way that skipped the tail of the graph.
            ctx.verification = ctx.verification or VerificationResult(
                outcome=VerificationOutcome.FAILED,
                claims_checked=len(ctx.claims),
                claims_supported=0,
                issues=[
                    VerificationIssue(
                        claim_id="run",
                        reason="incomplete_run",
                        detail="The graph ended without a verification result.",
                        suggested_query=None,
                    )
                ],
            )
            ctx.route_decision = ctx.route_decision or RouteDecision(
                route=Route.LEGAL_COUNSEL_REVIEW,
                basis="The advisory graph did not complete.",
                confidence=0.1,
                escalation_path=["compliance_desk", "employment_counsel"],
            )
        status = (
            RunStatus.ESCALATED
            if ctx.route_decision.route is Route.LEGAL_COUNSEL_REVIEW
            else RunStatus.COMPLETED
        )
        result = ctx.final_result(status=status)
        result.cached = False
        if trace_sink is not None:
            trace_sink.extend(ctx.trace)
        if evidence_sink is not None:
            evidence_sink.extend(ctx.evidence_list())
        if draft_sink is not None:
            # Evaluation may inspect the draft that was withheld on failure.
            # The public AdvisoryResult above still contains no failed claims.
            draft_sink["claims"] = list(ctx.withheld_claims or ctx.claims)
            draft_sink["citations"] = dict(ctx.withheld_citations or ctx.citations)
        logger.info(
            "advisory_completed",
            extra={
                "event": "advisory_completed",
                "fields": {
                    "run_id": run_id,
                    "status": status.value,
                    "route": result.route.value,
                    "domain": result.domain.value,
                    "variant": variant,
                    "steps": result.steps,
                    "tool_calls": result.tool_calls,
                    "claims": len(result.claims),
                    "verified": result.verification.outcome.value,
                    "issues": len(result.verification.issues),
                    "latency_ms": result.latency_ms,
                    "degraded": result.degraded,
                    "elapsed_s": round(time.perf_counter() - started, 3),
                },
            },
        )
    return result


def run_advisory_sync(request: ComplianceRequest, **kwargs: Any) -> AdvisoryResult:
    return asyncio.run(run_advisory(request, **kwargs))


__all__ = ["build_graph", "reset_graph", "run_advisory", "run_advisory_sync"]
