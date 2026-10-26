"""Claim-by-claim verification.

Verification is deterministic and independent of the drafting model.  A claim
survives only if (a) every cited passage was retrieved in this run, (b) an
operative citation is in force on the requested date, (c) the passage supports
the words and numbers of the claim, and (d) the fact pattern's qualifiers were
actually addressed.  The last check is what catches a plausible answer built on
the main section while the proviso that changes the result is missing.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from compliance.contracts import VerificationIssue, VerificationOutcome, VerificationResult
from compliance.observability import metrics
from compliance.util import match_tokens, numbers_in, parse_date

_SECTION_REF_RE = re.compile(
    r"\b(?:s|ss|sec|secs|section|sections)\.?\s*\d{1,3}[A-Z]?"
    r"(?:\s*\([^)]*\))*"
    r"(?:\s*(?:,|and)\s*\d{1,3}[A-Z]?(?:\s*\([^)]*\))*)*",
    re.I,
)
_DETERMINATION_RE = re.compile(
    r"\b(employee|he|she|they|the complainant|the accused)\s+(is|was|are|were)\s+"
    r"(guilty|liable|harassed|at fault)\b",
    re.I,
)


def _without_section_refs(text: str) -> str:
    """Remove citation markers before checking numbers.

    "s. 60(3)" contributes the digits 60 and 3 to a naive parser; neither is a
    substantive quantity in the claim.
    """
    return _SECTION_REF_RE.sub(" ", text)


def _numbers(text: str) -> set[int]:
    return numbers_in(_without_section_refs(text))


def _in_force_on(chunk: dict[str, Any], as_of: date) -> bool:
    start = parse_date(chunk.get("effective_from"))
    end = parse_date(chunk.get("effective_to"))
    return start is not None and start <= as_of and (end is None or as_of <= end)


def _support(claim_text: str, chunk_text: str) -> float:
    claim_tokens = match_tokens(claim_text)
    if not claim_tokens:
        return 0.0
    body = set(match_tokens(chunk_text))
    return sum(1 for token in claim_tokens if token in body) / len(claim_tokens)


def _fact_numbers(ctx) -> set[int]:
    """Numbers supplied by the caller are facts, not legal propositions.

    They must not be treated as hallucinations merely because the statute does
    not repeat the caller's headcount, wage, or service length.
    """
    request = ctx.request
    values: set[int] = set()
    for value in (
        request.establishment.employee_count,
        request.employment.completed_service_months,
        request.employment.service_days_last_12_months,
        request.employment.children_count,
    ):
        if isinstance(value, int):
            values.add(value)
    if request.employment.monthly_wage_inr is not None:
        values.add(int(request.employment.monthly_wage_inr))
    for value in (
        request.as_of_date,
        request.employment.start_date,
        request.employment.end_date,
        request.employment.expected_delivery_date,
    ):
        if isinstance(value, date):
            values.update({value.year, value.month, value.day})
    return values


_QUERY_FOR = {
    "ssc2020-s53-1-second-proviso": "gratuity five years not necessary death disablement expiry fixed term proviso",
    "ssc2020-s53-2-third-proviso": "gratuity pro rata fixed term employment third proviso",
    "ssc2020-s53-1-first-proviso": "working journalist gratuity three years first proviso",
    "ssc2020-sfirst-schedule-chapter-v": "gratuity applicability ten or more employees first schedule",
    "ssc2020-sfirst-schedule-chapter-vi": "maternity benefit applicability ten or more employees first schedule",
    "ssc2020-s164-1": "repeal Payment of Gratuity Act 1972 section 164 Code on Social Security",
    "posh2013-s4": "Internal Complaints Committee constitution POSH section 4",
    "ssc2020-s60": "maternity benefit eighty days twenty six weeks section 60",
    "gratuity1972-s4": "Payment of Gratuity Act 1972 section 4",
}


def verify(ctx) -> VerificationResult:
    issues: list[VerificationIssue] = []
    checks = [
        "citation_retrieved",
        "in_force_on_as_of",
        "passage_support",
        "numeric_consistency",
        "qualifier_completeness",
        "no_unauthorised_determination",
    ]
    supported_count = 0

    for claim in ctx.claims:
        if claim.kind == "escalation":
            claim.supported = True
            supported_count += 1
            continue
        claim_issues: list[VerificationIssue] = []
        if not claim.citations:
            claim_issues.append(
                VerificationIssue(
                    claim_id=claim.id,
                    reason="no_citation",
                    detail="An operative statement must cite at least one retrieved passage.",
                    suggested_query=ctx.request.question,
                )
            )
        cited_texts: list[str] = []
        for citation_id in claim.citations:
            chunk = ctx.evidence.get(citation_id)
            if chunk is None:
                claim_issues.append(
                    VerificationIssue(
                        claim_id=claim.id,
                        reason="citation_not_retrieved",
                        detail=f"Citation {citation_id!r} was not retrieved in this run.",
                        suggested_query="retrieve the cited provision directly",
                    )
                )
                continue
            cited_texts.append(chunk.get("text", ""))
            if claim.kind == "operative" and not _in_force_on(chunk, ctx.as_of):
                claim_issues.append(
                    VerificationIssue(
                        claim_id=claim.id,
                        reason="not_in_force_on_date",
                        detail=(
                            f"{chunk['citation']} is not in force on {ctx.as_of.isoformat()} "
                            f"({chunk.get('status')}); an operative claim may not rely on it."
                        ),
                        suggested_query=_QUERY_FOR.get(chunk["provision_id"], chunk["citation"]),
                    )
                )
        combined = " ".join(cited_texts)
        score = _support(claim.text, combined) if cited_texts else 0.0
        claim.support_score = round(score, 4)
        minimum = 0.45 if claim.kind == "operative" else 0.18
        if cited_texts and score < minimum:
            claim_issues.append(
                VerificationIssue(
                    claim_id=claim.id,
                    reason="weak_passage_support",
                    detail=(
                        f"Only {score:.0%} of the claim's content words occur in the cited passage "
                        f"(minimum {minimum:.0%})."
                    ),
                    suggested_query=_QUERY_FOR.get(
                        ctx.evidence[claim.citations[0]]["provision_id"],
                        ctx.request.question,
                    ),
                )
            )
        if claim.kind == "operative" and cited_texts:
            unsupported = _numbers(claim.text) - _numbers(combined) - _fact_numbers(ctx)
            # Section/appeal numbers are also digits; only flag a handful of
            # substantive numbers so a section reference cannot create noise.
            unsupported = {value for value in unsupported if value > 0 and len(str(value)) <= 6}
            if unsupported:
                claim_issues.append(
                    VerificationIssue(
                        claim_id=claim.id,
                        reason="number_not_supported",
                        detail=f"Number(s) {sorted(unsupported)} do not appear in the cited passage.",
                        suggested_query=_QUERY_FOR.get(
                            ctx.evidence[claim.citations[0]]["provision_id"],
                            ctx.request.question,
                        ),
                    )
                )
        if _DETERMINATION_RE.search(claim.text):
            claim_issues.append(
                VerificationIssue(
                    claim_id=claim.id,
                    reason="unauthorised_determination",
                    detail="The claim decides a contested fact or legal outcome.",
                    suggested_query=None,
                )
            )
        if claim_issues:
            claim.supported = False
            claim.reason = claim_issues[0].reason
            for issue in claim_issues:
                issues.append(issue)
                metrics.VERIFICATION_FAILURES.labels(reason=issue.reason).inc()
        else:
            claim.supported = True
            claim.support_score = max(claim.support_score, minimum)
            supported_count += 1

    # Completeness: a missing qualifier is a verification failure even when the
    # main section is perfectly quoted.
    from compliance.agents.policy import qualifier_provisions

    cited = ctx.cited_provision_ids()
    for provision_id, reason in qualifier_provisions(ctx).items():
        if provision_id in cited:
            continue
        issues.append(
            VerificationIssue(
                claim_id="qualifiers",
                reason="qualifier_missing",
                detail=f"{reason}; the provision is absent from the draft's operative citations.",
                suggested_query=_QUERY_FOR.get(provision_id, reason),
            )
        )
        metrics.VERIFICATION_FAILURES.labels(reason="qualifier_missing").inc()

    # A claim may not cite a repealed and an in-force provision as operative
    # support for the same proposition.
    for claim in ctx.claims:
        if claim.kind != "operative":
            continue
        statuses = {
            ctx.evidence[cid].get("status")
            for cid in claim.citations
            if cid in ctx.evidence
        }
        if "repealed" in statuses and "in_force" in statuses:
            issues.append(
                VerificationIssue(
                    claim_id=claim.id,
                    reason="conflicting_status",
                    detail="The claim mixes a repealed and an in-force provision as operative authority.",
                    suggested_query=None,
                )
            )
            claim.supported = False

    # De-duplicate issues while preserving order.
    seen: set[tuple[str, str, str]] = set()
    unique: list[VerificationIssue] = []
    for issue in issues:
        key = (issue.claim_id, issue.reason, issue.detail)
        if key in seen:
            continue
        seen.add(key)
        unique.append(issue)

    outcome = VerificationOutcome.PASSED if not unique else VerificationOutcome.FAILED
    return VerificationResult(
        outcome=outcome,
        claims_checked=len(ctx.claims),
        claims_supported=supported_count,
        issues=unique,
        loops=ctx.loop,
        checks=checks,
        notes=(
            "All operative claims cite retrieved, in-force provisions and the fact pattern's qualifiers were addressed."
            if not unique
            else "The draft is withheld from publication until the issues are resolved or counsel reviews it."
        ),
    )


__all__ = ["verify"]
