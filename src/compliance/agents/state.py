from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from compliance.config import settings
from compliance.contracts import (
    AdvisoryResult,
    Citation,
    Claim,
    ComplianceRequest,
    Domain,
    GuardrailReport,
    RouteDecision,
    RunStatus,
    Usage,
    UsageStep,
    VerificationResult,
)
from compliance.llm.base import LLMClient, LLMResult
from compliance.llm.prompts import PromptRegistry
from compliance.logging_setup import get_logger
from compliance.observability import metrics
from compliance.tools import Coverage, ToolBox
from compliance.util import iso_now

logger = get_logger(__name__)


class RunBudgetExceeded(RuntimeError):
    """The run exceeded its wall-clock, step, or model-call budget."""


@dataclass
class RunContext:
    """Mutable state shared by the agent nodes.

    LangGraph carries a single reference to this object.  Keeping the mutable
    state in one auditable object makes budget accounting and the trace exact:
    every node appends to the same timeline.
    """

    request: ComplianceRequest
    run_id: str
    correlation_id: str
    variant: str
    as_of: date
    toolbox: ToolBox
    llm: LLMClient
    prompts: PromptRegistry
    index_version: str | None = None
    guardrail_report: GuardrailReport = field(default_factory=GuardrailReport)

    created_at: str = field(default_factory=iso_now)
    started: float = field(default_factory=time.perf_counter)
    deadline: float = 0.0

    domain: Domain = Domain.OTHER
    risks: list[str] = field(default_factory=list)
    coverage: Coverage | None = None
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)
    evidence_order: list[str] = field(default_factory=list)
    evidence_queries: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    claims: list[Claim] = field(default_factory=list)
    citations: dict[str, Citation] = field(default_factory=dict)
    # A failed draft is not published, but it is retained for evaluation/debugging.
    withheld_claims: list[Claim] = field(default_factory=list)
    withheld_citations: dict[str, Citation] = field(default_factory=dict)
    verification: VerificationResult | None = None
    route_decision: RouteDecision | None = None

    usage: Usage = field(default_factory=Usage)
    steps: int = 0
    loop: int = 0
    llm_calls: int = 0
    degraded: bool = False
    errors: list[str] = field(default_factory=list)
    trace: list[dict[str, Any]] = field(default_factory=list)
    node_timings: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.deadline:
            self.deadline = self.started + settings.max_wall_clock_s

    # ------------------------------------------------------------------ budget
    def remaining_s(self) -> float:
        return max(0.0, self.deadline - time.perf_counter())

    def check_budget(self) -> None:
        if self.remaining_s() <= 0:
            raise RunBudgetExceeded(f"wall-clock budget of {settings.max_wall_clock_s}s exhausted")
        if self.steps >= settings.max_steps:
            raise RunBudgetExceeded(f"step budget of {settings.max_steps} exhausted")

    # ------------------------------------------------------------------- trace
    def event(
        self,
        kind: str,
        *,
        agent: str | None = None,
        name: str | None = None,
        step: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "seq": len(self.trace) + 1,
            "ts": iso_now(),
            "elapsed_ms": round((time.perf_counter() - self.started) * 1000, 2),
            "kind": kind,
            "agent": agent,
            "name": name,
            "data": data or {},
        }
        if step:
            payload["step"] = step
        self.trace.append(payload)

    # -------------------------------------------------------------------- usage
    def record_usage(self, step: str, result: LLMResult, prompt_version: str) -> None:
        self.usage.prompt_tokens += result.prompt_tokens
        self.usage.completion_tokens += result.completion_tokens
        self.usage.total_tokens += result.total_tokens
        self.usage.cost_usd += result.cost_usd
        self.usage.llm_calls += 1
        self.usage.per_step.append(
            UsageStep(
                step=step,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                cost_usd=round(result.cost_usd, 8),
                latency_ms=round(result.latency_ms, 2),
                provider=result.provider,
                model=result.model,
                prompt_version=prompt_version,
            )
        )
        if result.fallback_used:
            self.degraded = True
            self.event("degraded", agent=step, name="llm_fallback", data={"error": result.error})

    async def complete(
        self,
        *,
        step: str,
        task: str,
        prompt: str,
        max_tokens: int = 700,
    ) -> LLMResult | None:
        """Call the model within the run's call and wall-clock budgets."""
        if self.llm_calls >= settings.max_llm_calls:
            self.errors.append(f"llm_call_budget_exhausted:{step}")
            return None
        if self.remaining_s() <= 0.25:
            self.errors.append(f"wall_clock_exhausted_before_llm:{step}")
            return None
        self.llm_calls += 1
        version = self.prompts.get(task).version
        timeout = max(0.5, min(settings.llm_timeout_s * 2, self.remaining_s() - 0.2))
        try:
            result = await asyncio.wait_for(
                self.llm.complete(
                    task=task,
                    prompt=prompt,
                    prompt_version=version,
                    temperature=0.0,
                    max_tokens=max_tokens,
                    json_mode=True,
                ),
                timeout=timeout,
            )
        except TimeoutError:
            self.degraded = True
            self.errors.append(f"llm_timeout:{step}")
            self.event("llm_error", agent=step, name="timeout", data={"timeout_s": round(timeout, 2)})
            metrics.PROVIDER_FAILURES.labels(reason="run_timeout").inc()
            return None
        except Exception as exc:  # provider failure is a degraded path, not a crash
            self.degraded = True
            self.errors.append(f"llm_error:{step}:{type(exc).__name__}")
            self.event("llm_error", agent=step, name=type(exc).__name__, data={"error": str(exc)[:300]})
            return None
        self.record_usage(step, result, version)
        self.event(
            "llm_call",
            agent=step,
            name=task,
            data={
                "provider": result.provider,
                "model": result.model,
                "prompt_version": version,
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
                "cost_usd": round(result.cost_usd, 8),
                "latency_ms": round(result.latency_ms, 2),
                "fallback": result.fallback_used,
            },
        )
        return result

    # --------------------------------------------------------------- lifecycle
    def start_step(self, name: str) -> float:
        self.steps += 1
        self.check_budget()
        self.event("step_start", agent=name, step=name)
        return time.perf_counter()

    def end_step(self, name: str, started: float, **data: Any) -> None:
        elapsed = time.perf_counter() - started
        self.node_timings[name] = round(elapsed * 1000, 2)
        metrics.AGENT_DURATION.labels(agent=name).observe(elapsed)
        metrics.AGENT_SUCCESS.labels(agent=name, outcome="ok").inc()
        self.event("step_end", agent=name, step=name, data={"duration_ms": round(elapsed * 1000, 2), **data})

    def add_evidence(self, chunk: dict[str, Any], *, reason: str, score: float = 0.0) -> None:
        chunk_id = chunk["id"]
        if chunk_id in self.evidence:
            existing = self.evidence[chunk_id]
            if score > existing.get("evidence_score", 0.0):
                existing["evidence_score"] = score
            return
        item = dict(chunk)
        item["evidence_score"] = score
        item["retrieval_reason"] = reason
        item["retrieved_rank"] = len(self.evidence_order) + 1
        self.evidence[chunk_id] = item
        self.evidence_order.append(chunk_id)

    def evidence_list(self) -> list[dict[str, Any]]:
        return [self.evidence[cid] for cid in self.evidence_order]

    def best_for(self, provision_id: str) -> dict[str, Any] | None:
        exact = [self.evidence[cid] for cid in self.evidence_order if self.evidence[cid]["provision_id"] == provision_id]
        if not exact:
            return None
        return max(exact, key=lambda item: item.get("evidence_score", 0.0))

    def best_for_text(self, provision_id: str, claim_text: str) -> dict[str, Any] | None:
        """Pick the chunk of a provision that actually supports this claim.

        Long sections are split into several chunks.  Citing the highest-ranked
        chunk is not enough if the proposition comes from a later part; this
        chooses by lexical support, with retrieval score as a tie-breaker.
        """
        from compliance.util import match_tokens

        exact = [self.evidence[cid] for cid in self.evidence_order if self.evidence[cid]["provision_id"] == provision_id]
        if not exact:
            return None
        claim_tokens = set(match_tokens(claim_text))
        if not claim_tokens:
            return self.best_for(provision_id)

        def score(item: dict[str, Any]) -> tuple[float, float]:
            body = set(match_tokens(item.get("text", "")))
            overlap = len(claim_tokens & body) / len(claim_tokens)
            return (overlap, item.get("evidence_score", 0.0))

        return max(exact, key=score)

    def has_provision(self, provision_id: str) -> bool:
        return any(self.evidence[cid]["provision_id"] == provision_id for cid in self.evidence_order)

    def cited_provision_ids(self) -> set[str]:
        return {
            self.evidence[cid]["provision_id"]
            for claim in self.claims
            for cid in claim.citations
            if cid in self.evidence
        }

    def final_result(self, *, status: RunStatus) -> AdvisoryResult:
        assert self.route_decision is not None
        assert self.verification is not None
        advisory, blocked = _compose_advisory_markdown(self)
        if blocked:
            self.guardrail_report.output_filtered_phrases.extend(blocked)
        completed = iso_now()
        latency_ms = (time.perf_counter() - self.started) * 1000
        metrics.RUNS_TOTAL.labels(
            status=status.value, route=self.route_decision.route.value, domain=self.domain.value
        ).inc()
        metrics.RUN_DURATION.observe(latency_ms / 1000)
        metrics.RUN_COST.observe(self.usage.cost_usd)
        metrics.STEPS_PER_RUN.observe(self.steps)
        metrics.TOOL_CALLS_PER_RUN.observe(self.toolbox.calls)
        metrics.VERIFICATION_TOTAL.labels(outcome=self.verification.outcome.value).inc()
        if self.degraded:
            metrics.RUNS_DEGRADED_TOTAL.inc()
        if self.route_decision.route.value == "legal_counsel_review":
            metrics.ESCALATIONS_TOTAL.labels(
                reason=self.verification.issues[0].reason if self.verification.issues else "policy",
                route=self.route_decision.route.value,
            ).inc()
        return AdvisoryResult(
            run_id=self.run_id,
            correlation_id=self.correlation_id,
            status=status,
            route=self.route_decision.route,
            domain=self.domain,
            advisory=advisory,
            summary=self.summary,
            claims=self.claims,
            citations=list(self.citations.values()),
            verification=self.verification,
            route_decision=self.route_decision,
            guardrails=self.guardrail_report,
            prompt_versions=self.prompts.versions(self.variant),
            usage=self.usage,
            latency_ms=round(latency_ms, 2),
            steps=self.steps,
            tool_calls=self.toolbox.calls,
            degraded=self.degraded,
            index_version=self.index_version,
            created_at=self.created_at,
            completed_at=completed,
        )


def _compose_advisory_markdown(ctx: RunContext) -> tuple[str, list[str]]:
    """Render the answer.  The output filter runs over the rendered text."""
    from compliance.guardrails import filter_output

    assert ctx.route_decision is not None
    refs: dict[str, int] = {}
    ordered = sorted(
        ctx.citations.values(),
        key=lambda c: (c.retrieved_rank if c.retrieved_rank is not None else 999, c.citation),
    )
    for index, citation in enumerate(ordered, start=1):
        refs[citation.id] = index

    def marker(claim: Claim) -> str:
        numbers = sorted({refs[cid] for cid in claim.citations if cid in refs})
        return "".join(f"[{n}]" for n in numbers)

    lines: list[str] = []
    lines.append(f"**Summary**\n\n{ctx.summary}\n")
    if ordered:
        lines.append("**Provisions considered**\n")
        for index, citation in enumerate(ordered, start=1):
            lines.append(
                f"{index}. {citation.citation} — {citation.status.replace('_', ' ')}"
                + (
                    f" (repealed with effect from {citation.effective_to.isoformat()} by {citation.repealed_by})"
                    if citation.status == "repealed" and citation.effective_to
                    else ""
                )
                + f". \"{citation.quote}\""
            )
    if ctx.claims:
        lines.append("\n**Analysis**\n")
        for claim in ctx.claims:
            label = {
                "operative": "",
                "background": "Background: ",
                "procedural": "Procedure: ",
                "escalation": "Escalation: ",
            }.get(claim.kind, "")
            lines.append(f"- {label}{claim.text} {marker(claim)}".rstrip())
    if ctx.route_decision.required_fields:
        lines.append("\n**Information required**\n")
        for field_name in ctx.route_decision.required_fields:
            lines.append(f"- `{field_name}`")
    if ctx.route_decision.next_actions:
        lines.append("\n**Next steps**\n")
        for action in ctx.route_decision.next_actions:
            lines.append(f"- {action}")
    lines.append("\n**Scope**\n")
    lines.append(
        "This desk cites enacted provisions and applies them to the facts supplied. It does not "
        "determine disputed facts, decide a harassment complaint, or issue a legal opinion."
    )
    lines.append("\n> This is a cited compliance advisory, not legal advice or a legal opinion. "
                 "A qualified professional must decide the matter.")
    text, blocked = filter_output("\n".join(lines))
    return text, blocked


__all__ = ["RunBudgetExceeded", "RunContext"]
