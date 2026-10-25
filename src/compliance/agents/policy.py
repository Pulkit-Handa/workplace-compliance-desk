"""Deterministic legal policy used by the advisory agents.

The model is allowed to propose queries and prose, but it is never the source of
a legal rule.  This module turns the request facts plus retrieved passages into
claims whose citations can be checked one by one.  When a rule or qualifier is
absent from the evidence, the composer deliberately does *not* invent it: the
verifier will report the gap and the run will escalate.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from compliance.contracts import (
    Citation,
    Claim,
    ComplianceRequest,
    Domain,
    Route,
    RouteDecision,
    SeparationEvent,
)
from compliance.tools import SSC_COMMENCEMENT, Coverage
from compliance.util import normalise_text

GRATUITY_WORDS = (
    "gratuity", "retirement", "resignation", "superannuation", "continuous service",
    "five years", "fixed term", "fixed-term", "last drawn", "service benefit",
)
MATERNITY_WORDS = (
    "maternity", "pregnan", "delivery", "childbirth", "crèche", "creche", "nursing break",
    "medical bonus", "miscarriage", "adoption", "commissioning mother", "26 weeks",
    "twenty-six weeks",
)
HARASSMENT_WORDS = (
    "harass", "posh", "sexual", "internal committee", "internal complaints committee",
    "icc", "workplace safety", "hostile environment", "misconduct of a sexual nature",
)
REPEAL_WORDS = (
    "1972", "payment of gratuity act", "old act", "repealed", "repeal", "still in force",
    "still applicable", "which act", "successor", "transition",
)
COMPLAINT_WORDS = (
    "allegation", "alleged", "incident", "complaint", "complained", "accused", "investigate",
    "inquiry", "terminate", "dismiss", "fire", "penalty for", "punish", "misconduct",
)
PROCEDURE_WORDS = (
    "how should", "how do", "procedure", "process", "timeline", "deadline", "compose",
    "constitute", "composition", "policy", "training", "annual report", "confidentiality",
)


@dataclass
class QuerySpec:
    query: str
    reason: str
    top_k: int = 4


@dataclass
class DraftData:
    summary: str
    claims: list[Claim] = field(default_factory=list)
    citations: dict[str, Citation] = field(default_factory=dict)


def classify(request: ComplianceRequest) -> tuple[Domain, list[str]]:
    text = normalise_text(" ".join(filter(None, [request.question, request.notes or ""]))).lower()

    def score(words: tuple[str, ...]) -> int:
        return sum(2 if len(word.split()) > 1 else 1 for word in words if word in text)

    scores = {
        Domain.GRATUITY: score(GRATUITY_WORDS),
        Domain.MATERNITY_BENEFIT: score(MATERNITY_WORDS),
        Domain.WORKPLACE_HARASSMENT: score(HARASSMENT_WORDS),
    }
    domain = max(scores, key=scores.get)
    if scores[domain] == 0:
        domain = Domain.OTHER

    risks: list[str] = []
    if domain is Domain.WORKPLACE_HARASSMENT:
        # A general policy question ("what is the complaint timeline?") is not a
        # specific complaint.  Escalate only when the text points at an actual
        # allegation/incident or asks the desk to act against a person.
        direct_complaint = any(
            word in text for word in COMPLAINT_WORDS if word != "complaint"
        )
        complaint_about_person = "complaint" in text and any(
            word in text for word in ("employee", "manager", "against", "alleged", "harass")
        )
        procedural = any(word in text for word in PROCEDURE_WORDS)
        if (direct_complaint or complaint_about_person) and not (
            procedural and not direct_complaint
        ):
            risks.append("harassment_complaint")
        if re.search(r"\b(can|could|should)\s+we\s+(terminate|dismiss|fire|punish)\b", text):
            risks.append("harassment_complaint")
    if re.search(r"\b(unfair|wrongful|illegal)\s+(termination|dismissal|retrenchment)\b", text):
        risks.append("termination_dispute")
    if re.search(r"\b(should|must)\s+(we|the company)\s+(terminate|dismiss|fire)\b", text):
        risks.append("termination_dispute")
    if re.search(r"\b(is it legal|are we liable|will we win|legal opinion|sue|litigate)\b", text):
        risks.append("legal_opinion_request")
    return domain, sorted(set(risks))


def query_plan(ctx, *, missing: list[str] | None = None) -> list[QuerySpec]:
    request = ctx.request
    as_of: date = ctx.as_of
    domain = ctx.domain
    base = normalise_text(request.question)
    if request.notes:
        base = normalise_text(base + " " + request.notes)
    plans: list[QuerySpec] = []
    if missing:
        for query in missing[:4]:
            plans.append(QuerySpec(query=query, reason="verification_gap", top_k=4))
        return plans
    if ctx.variant == "v1":
        plans.append(QuerySpec(query=base, reason="single_pass_baseline", top_k=6))
        return plans

    plans.append(QuerySpec(query=base, reason="question", top_k=4))
    if domain is Domain.GRATUITY:
        if as_of >= SSC_COMMENCEMENT:
            plans.extend(
                [
                    QuerySpec(
                        query="gratuity five years not necessary death disablement expiration fixed term employment proviso",
                        reason="qualifier: five-year waiver",
                        top_k=4,
                    ),
                    QuerySpec(
                        query="gratuity pro rata fixed term employment fifteen days wages calculation",
                        reason="qualifier: pro-rata rate",
                        top_k=4,
                    ),
                    QuerySpec(
                        query="continuous service two hundred forty days definition",
                        reason="definition: continuous service",
                        top_k=3,
                    ),
                    QuerySpec(
                        query="gratuity applicability ten or more employees first schedule shop establishment",
                        reason="threshold: coverage",
                        top_k=3,
                    ),
                    QuerySpec(
                        query="repeal Payment of Gratuity Act 1972 section 164 Code on Social Security commencement",
                        reason="currency: repeal and successor",
                        top_k=3,
                    ),
                ]
            )
        else:
            plans.append(
                QuerySpec(
                    query="Payment of Gratuity Act 1972 gratuity five years continuous service",
                    reason="historical regime",
                    top_k=5,
                )
            )
    elif domain is Domain.MATERNITY_BENEFIT:
        plans.extend(
            [
                QuerySpec(
                    query="maternity benefit eighty days twenty-six weeks twelve weeks two children",
                    reason="qualifier: eligibility and duration",
                    top_k=4,
                ),
                QuerySpec(
                    query="maternity benefit applicability ten or more employees first schedule",
                    reason="threshold: coverage",
                    top_k=3,
                ),
                QuerySpec(
                    query="creche facility fifty employees nursing breaks medical bonus",
                    reason="qualifier: ancillary benefits",
                    top_k=3,
                ),
                QuerySpec(
                    query="dismissal absence pregnancy protection maternity benefit",
                    reason="qualifier: dismissal protection",
                    top_k=3,
                ),
            ]
        )
    elif domain is Domain.WORKPLACE_HARASSMENT:
        plans.extend(
            [
                QuerySpec(
                    query="Internal Complaints Committee constitution members employer duty",
                    reason="qualifier: committee",
                    top_k=3,
                ),
                QuerySpec(
                    query="complaint three months limitation extension Internal Committee",
                    reason="qualifier: limitation",
                    top_k=3,
                ),
                QuerySpec(
                    query="inquiry procedure confidentiality annual report",
                    reason="qualifier: procedure",
                    top_k=3,
                ),
                QuerySpec(
                    query="penalty non-compliance fifty thousand rupees",
                    reason="qualifier: penalty",
                    top_k=3,
                ),
            ]
        )
    return plans


def required_provisions(ctx) -> list[str]:
    """Provisions useful to ground a complete answer (used for fetching)."""
    request = ctx.request
    emp = request.employment
    domain = ctx.domain
    as_of: date = ctx.as_of
    required: list[str] = []
    if domain is Domain.GRATUITY:
        if as_of >= SSC_COMMENCEMENT:
            required += [
                "ssc2020-s53-1",
                "ssc2020-s53-2",
                "ssc2020-sfirst-schedule-chapter-v",
                "ssc2020-s164-1",
                "commencement2025-ss-o-5319-e",
            ]
            if emp.event in (SeparationEvent.DEATH, SeparationEvent.DISABLEMENT) or (
                emp.event == SeparationEvent.EXPIRY
            ):
                required += ["ssc2020-s53-1-second-proviso"]
            if emp.worker_type is not None and emp.worker_type.value == "fixed_term":
                required += ["ssc2020-s53-2-third-proviso", "ssc2020-s2-34"]
            if emp.event in (
                SeparationEvent.RESIGNATION,
                SeparationEvent.RETIREMENT,
                SeparationEvent.SUPERANNUATION,
                SeparationEvent.TERMINATION,
                SeparationEvent.EXPIRY,
            ):
                required += ["ssc2020-s54"]
            if emp.event in (
                SeparationEvent.TERMINATION,
                SeparationEvent.EXPIRY,
                SeparationEvent.RESIGNATION,
            ):
                required += ["ssc2020-s53-6"]
        else:
            required += ["gratuity1972-s1", "gratuity1972-s4"]
    elif domain is Domain.MATERNITY_BENEFIT:
        required += ["ssc2020-s60", "ssc2020-sfirst-schedule-chapter-vi"]
        if request.establishment.employee_count is not None and request.establishment.employee_count >= 50:
            required.append("ssc2020-s67")
        if emp.event == SeparationEvent.TERMINATION:
            required.append("ssc2020-s68")
        required += ["ssc2020-s64", "ssc2020-s66"]
    elif domain is Domain.WORKPLACE_HARASSMENT:
        required += ["posh2013-s4", "posh2013-s9", "posh2013-s11", "posh2013-s26"]
    return list(dict.fromkeys(required))


def qualifier_provisions(ctx) -> dict[str, str]:
    """Provisions whose absence would make an answer incomplete or misleading.

    Returns a map provision_id -> reason.  This is the completeness check that a
    single-pass baseline so often fails: the main section is present but the
    proviso that changes the answer is not.
    """
    request = ctx.request
    emp = request.employment
    text = normalise_text(request.question + " " + (request.notes or "")).lower()
    result: dict[str, str] = {}
    if ctx.domain is Domain.GRATUITY:
        if ctx.as_of >= SSC_COMMENCEMENT:
            if emp.event in (SeparationEvent.DEATH, SeparationEvent.DISABLEMENT):
                result["ssc2020-s53-1-second-proviso"] = (
                    "death or disablement waives the five-year condition under the second proviso to s. 53(1)"
                )
            if emp.event == SeparationEvent.EXPIRY:
                result["ssc2020-s53-1-second-proviso"] = (
                    "expiration of a fixed term employment waives the five-year condition "
                    "under the second proviso to s. 53(1)"
                )
            if emp.worker_type is not None and emp.worker_type.value == "fixed_term":
                result["ssc2020-s53-2-third-proviso"] = (
                    "fixed term employment is paid pro rata under the third proviso to s. 53(2)"
                )
            if "journalist" in text:
                result["ssc2020-s53-1-first-proviso"] = (
                    "the first proviso to s. 53(1) reduces five years to three years for working journalists"
                )
            result["ssc2020-sfirst-schedule-chapter-v"] = "the Chapter V coverage threshold must be addressed"
            if any(word in text for word in REPEAL_WORDS):
                result["ssc2020-s164-1"] = "the repeal of the 1972 Act must be addressed explicitly"
        else:
            result["gratuity1972-s4"] = "the 1972 Act is the regime in force on the requested date"
    elif ctx.domain is Domain.MATERNITY_BENEFIT:
        result["ssc2020-s60"] = "eligibility and duration are set by s. 60"
        result["ssc2020-sfirst-schedule-chapter-vi"] = "the Chapter VI coverage threshold must be addressed"
    elif ctx.domain is Domain.WORKPLACE_HARASSMENT:
        result["posh2013-s4"] = "the Internal Complaints Committee duty is the core POSH obligation"
    return result


# --------------------------------------------------------------------- drafting
def _citation_from_chunk(ctx, chunk: dict[str, Any], claim_text: str) -> Citation:
    from compliance.util import match_tokens

    quote = normalise_text(chunk.get("body") or chunk.get("text") or "")
    if len(quote) > 520:
        quote = quote[:517].rstrip() + "…"
    body_tokens = set(match_tokens(chunk.get("text") or ""))
    claim_tokens = match_tokens(claim_text)
    support = (
        sum(1 for token in claim_tokens if token in body_tokens) / len(claim_tokens)
        if claim_tokens
        else 0.0
    )
    existing = ctx.citations.get(chunk["id"])
    if existing is None:
        existing = Citation(
            id=chunk["id"],
            citation=chunk["citation"],
            statute=chunk["short_title"],
            section=chunk["section"],
            clause=chunk.get("clause") or None,
            heading=chunk.get("heading") or None,
            status=chunk["status"],
            effective_from=_date_or_none(chunk.get("effective_from")),
            effective_to=_date_or_none(chunk.get("effective_to")),
            repealed_by=chunk.get("repealed_by") or None,
            quote=quote,
            source_url=chunk.get("source_url", ""),
            retrieved_rank=chunk.get("retrieved_rank"),
            support_score=round(support, 4),
        )
        ctx.citations[chunk["id"]] = existing
    else:
        existing.support_score = max(existing.support_score, round(support, 4))
    return existing


def _date_or_none(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _add_claim(ctx, text: str, provision_ids: list[str], *, kind: str = "operative") -> Claim | None:
    citations: list[str] = []
    for provision_id in provision_ids:
        chunk = ctx.best_for_text(provision_id, text)
        if chunk is None:
            continue
        citations.append(chunk["id"])
        _citation_from_chunk(ctx, chunk, text)
    citations = list(dict.fromkeys(citations))
    if kind != "escalation" and not citations:
        return None
    claim = Claim(
        id=f"clm_{len(ctx.claims) + 1:02d}",
        text=text,
        citations=citations,
        kind=kind,  # type: ignore[arg-type]
    )
    ctx.claims.append(claim)
    return claim


def compose(ctx) -> DraftData:
    request = ctx.request
    domain = ctx.domain
    emp = request.employment
    coverage: Coverage | None = ctx.coverage
    months = emp.completed_service_months
    wage = emp.monthly_wage_inr
    fixed_term = emp.worker_type is not None and emp.worker_type.value == "fixed_term"
    text = normalise_text(request.question + " " + (request.notes or "")).lower()
    repeal_asked = any(word in text for word in REPEAL_WORDS)

    if domain is Domain.GRATUITY:
        if ctx.as_of >= SSC_COMMENCEMENT:
            _compose_gratuity_current(ctx, coverage, months, wage, fixed_term, repeal_asked)
        else:
            _compose_gratuity_historical(ctx, coverage, months, wage)
    elif domain is Domain.MATERNITY_BENEFIT:
        _compose_maternity(ctx, coverage, months)
    elif domain is Domain.WORKPLACE_HARASSMENT:
        _compose_harassment(ctx)
    else:
        _add_claim(
            ctx,
            "The request does not identify a provision in the indexed corpus that answers it. "
            "The desk does not answer outside its indexed sources.",
            [],
            kind="escalation",
        )

    ctx.summary = _summary(ctx)
    return DraftData(summary=ctx.summary, claims=ctx.claims, citations=ctx.citations)


def _compose_gratuity_current(
    ctx,
    coverage: Coverage | None,
    months: int | None,
    wage: float | None,
    fixed_term: bool,
    repeal_asked: bool,
) -> None:
    request = ctx.request
    emp = request.employment
    event = emp.event
    waived = event in (SeparationEvent.DEATH, SeparationEvent.DISABLEMENT) or event == SeparationEvent.EXPIRY
    below_threshold = months is not None and not waived and months < 60

    # Coverage is a precondition, not a detail.
    if coverage is not None:
        if coverage.covered is True:
            _add_claim(
                ctx,
                "The establishment is within Chapter V (Gratuity): "
                + coverage.reason
                + " (First Schedule, Chapter V).",
                ["ssc2020-sfirst-schedule-chapter-v"],
            )
        elif coverage.covered is False:
            _add_claim(
                ctx,
                f"Chapter V (Gratuity) does not apply on the stated headcount of {coverage.employee_count}: "
                "a shop or establishment is covered only when ten or more employees were employed on any day "
                "of the preceding twelve months (First Schedule, Chapter V).",
                ["ssc2020-sfirst-schedule-chapter-v"],
            )
        else:
            _add_claim(
                ctx,
                "Coverage under Chapter V (Gratuity) is not established: a shop or establishment is covered "
                "when ten or more employees were employed on any day of the preceding twelve months, and the "
                "headcount was not supplied (First Schedule, Chapter V).",
                ["ssc2020-sfirst-schedule-chapter-v"],
            )

    if event is SeparationEvent.NONE:
        _add_claim(
            ctx,
            "Gratuity is payable on termination of employment, not while employment subsists; the five-year "
            "continuous-service threshold in s. 53(1) is assessed when the employment ends.",
            ["ssc2020-s53-1"],
        )
        if fixed_term:
            _add_claim(
                ctx,
                "On termination of the contract period of a fixed term employee, gratuity is payable under "
                "s. 53(1)(d), and is calculated on a pro rata basis under the third proviso to s. 53(2).",
                ["ssc2020-s53-1", "ssc2020-s53-2-third-proviso"],
            )
    else:
        _add_claim(
            ctx,
            "Gratuity is payable to an employee on termination of employment after continuous service of not "
            "less than five years, on superannuation, retirement or resignation, death or disablement, or "
            "termination of a fixed term contract (s. 53(1)).",
            ["ssc2020-s53-1"],
        )
        if waived:
            reason = {
                SeparationEvent.DEATH: "death",
                SeparationEvent.DISABLEMENT: "disablement",
                SeparationEvent.EXPIRY: "expiration of the fixed term employment",
            }[event]
            _add_claim(
                ctx,
                f"The five-year continuous-service condition does not apply because termination is due to "
                f"{reason} (second proviso to s. 53(1)).",
                ["ssc2020-s53-1-second-proviso"],
            )
        if fixed_term:
            _add_claim(
                ctx,
                "For an employee employed on fixed term employment, the employer must pay gratuity on a pro "
                "rata basis (third proviso to s. 53(2)).",
                ["ssc2020-s53-2-third-proviso"],
            )

        if below_threshold:
            event_label = (
                emp.event.value
                if emp.event
                in (
                    SeparationEvent.RESIGNATION,
                    SeparationEvent.RETIREMENT,
                    SeparationEvent.SUPERANNUATION,
                    SeparationEvent.TERMINATION,
                )
                else "termination"
            )
            _add_claim(
                ctx,
                f"Gratuity is payable on termination of employment after continuous service of not less than "
                f"five years (s. 53(1)). The stated completed service of {months} months is less than five "
                f"years, so gratuity is not payable on {event_label}.",
                ["ssc2020-s53-1"],
            )
        elif months is not None:
            _add_claim(
                ctx,
                f"The stated completed service is {months} months. Continuous service is defined in s. 54; for "
                "a non-seasonal employee who is not in uninterrupted service, 240 days of actual work in the "
                "preceding twelve months is deemed continuous service.",
                ["ssc2020-s54"],
            )

        # Rate, arithmetic, and forfeiture are only meaningful once the
        # five-year threshold is met (or waived).  Showing a calculation to an
        # employee who is not yet entitled is misleading even when the prose
        # says "not payable".
        if not below_threshold:
            _add_claim(
                ctx,
                "For every completed year of service, or part in excess of six months, the rate is fifteen days' "
                "wages based on the wages last drawn by the employee (s. 53(2)).",
                ["ssc2020-s53-2"],
            )
            if months is not None and wage:
                years = months // 12 + (1 if months % 12 > 6 else 0)
                if fixed_term:
                    amount = (15 / 26) * wage * (months / 12)
                    basis = f"pro rata for {months} months"
                else:
                    amount = (15 / 26) * wage * years
                    basis = f"{years} completed year(s) after rounding a part over six months"
                _add_claim(
                    ctx,
                    f"Indicative arithmetic only: at a monthly wage of ₹{wage:,.0f}, 15/26 × wage × service gives "
                    f"about ₹{amount:,.0f} ({basis}). The statutory calculation uses the wages last drawn.",
                    ["ssc2020-s53-2"],
                    kind="procedural",
                )
            if ctx.has_provision("ssc2020-s53-6") and not any(
                "forfeit" in claim.text.lower() for claim in ctx.claims
            ):
                _add_claim(
                    ctx,
                    "Section 53(6) permits forfeiture to the extent of damage or loss caused, and wholly or partly "
                    "for riotous or disorderly conduct or an offence involving moral turpitude committed in the "
                    "course of employment.",
                    ["ssc2020-s53-6"],
                    kind="procedural",
                )

    # The repeal note is deliberate: it is the only correct way to keep the 1972
    # Act in the corpus without letting it become operative law.
    if ctx.variant == "v2" or repeal_asked:
        _add_claim(
            ctx,
            "The Payment of Gratuity Act, 1972, including its s. 4, is repealed. Section 164(1) of the Code "
            "on Social Security, 2020 repeals that Act, and it is not the operative statute for a termination "
            "on or after 21 November 2025. It is retained in the corpus for historical and transitional "
            "questions only.",
            ["ssc2020-s164-1", "commencement2025-ss-o-5319-e", "gratuity1972-s4"],
            kind="background",
        )


def _compose_gratuity_historical(
    ctx, coverage: Coverage | None, months: int | None, wage: float | None
) -> None:
    request = ctx.request
    emp = request.employment
    event = emp.event
    _add_claim(
        ctx,
        "As of the requested date, gratuity is governed by the Payment of Gratuity Act, 1972, which is in "
        "force until 20 November 2025; the Code on Social Security, 2020 commences on 21 November 2025.",
        ["gratuity1972-s4"],
        kind="background",
    )
    if coverage is not None:
        _add_claim(
            ctx,
            "The 1972 Act applies to the establishment on the stated facts; the ten-employee coverage rule "
            "for shops and establishments is in s. 1 of that Act.",
            ["gratuity1972-s1"],
        )
    _add_claim(
        ctx,
        "Gratuity is payable on termination after continuous service of not less than five years, on "
        "superannuation, retirement or resignation, or death or disablement (s. 4(1) of the 1972 Act).",
        ["gratuity1972-s4"],
    )
    if event in (SeparationEvent.DEATH, SeparationEvent.DISABLEMENT):
        _add_claim(
            ctx,
            "The completion of five years' continuous service is not necessary where termination is due to "
            "death or disablement (proviso to s. 4(1)).",
            ["gratuity1972-s4"],
        )
    if months is not None and months < 60 and event not in (SeparationEvent.DEATH, SeparationEvent.DISABLEMENT):
        event_label = event.value if event is not None else "termination"
        _add_claim(
            ctx,
            "Gratuity is payable on termination of employment after continuous service of not less than "
            f"five years (s. 4(1) of the 1972 Act). The stated completed service of {months} months is "
            f"less than five years, so gratuity is not payable on {event_label}.",
            ["gratuity1972-s4"],
        )
    if wage and months is not None and (
        months >= 60 or event in (SeparationEvent.DEATH, SeparationEvent.DISABLEMENT)
    ):
        years = months // 12 + (1 if months % 12 > 6 else 0)
        amount = (15 / 26) * wage * years
        _add_claim(
            ctx,
            f"Indicative arithmetic under s. 4(2): 15/26 × ₹{wage:,.0f} × {years} year(s) = about "
            f"₹{amount:,.0f}, using the wages last drawn.",
            ["gratuity1972-s4"],
            kind="procedural",
        )


def _compose_maternity(ctx, coverage: Coverage | None, months: int | None) -> None:
    request = ctx.request
    emp = request.employment
    if ctx.as_of < SSC_COMMENCEMENT:
        _add_claim(
            ctx,
            "Chapter VI of the Code on Social Security, 2020 is not in force on the requested date. The desk's "
            "corpus does not contain the Maternity Benefit Act, 1961, which governed on that date, so it cannot "
            "give a cited answer and escalates the request.",
            [],
            kind="escalation",
        )
        return
    if coverage is not None:
        if coverage.covered is True:
            _add_claim(
                ctx,
                "The establishment is within Chapter VI (Maternity Benefit): " + coverage.reason,
                ["ssc2020-sfirst-schedule-chapter-vi"],
            )
        elif coverage.covered is False:
            _add_claim(
                ctx,
                f"Chapter VI (Maternity Benefit) does not apply on the stated headcount of "
                f"{coverage.employee_count}: a shop or establishment is covered at ten or more employees.",
                ["ssc2020-sfirst-schedule-chapter-vi"],
            )
        else:
            _add_claim(
                ctx,
                "Coverage under Chapter VI (Maternity Benefit) is not established because the headcount was "
                "not supplied; a shop or establishment is covered at ten or more employees.",
                ["ssc2020-sfirst-schedule-chapter-vi"],
            )
    _add_claim(
        ctx,
        "A woman is entitled to maternity benefit only if she actually worked in the establishment for not "
        "less than eighty days in the twelve months immediately preceding the expected date of delivery "
        "(s. 60(2)).",
        ["ssc2020-s60"],
    )
    if emp.service_days_last_12_months is not None:
        days = emp.service_days_last_12_months
        if days < 80:
            _add_claim(
                ctx,
                f"On the stated {days} days of actual work in the last twelve months, the eighty-day "
                "eligibility condition in s. 60(2) is not met.",
                ["ssc2020-s60"],
            )
        else:
            _add_claim(
                ctx,
                f"The woman has actually worked in the establishment for not less than eighty days in the "
                f"twelve months immediately preceding the expected date of delivery (s. 60(2)). The stated "
                f"{days} days of actual work satisfies that condition.",
                ["ssc2020-s60"],
            )
    _add_claim(
        ctx,
        "The maximum maternity benefit is twenty-six weeks, of which not more than eight weeks may precede "
        "the expected date of delivery (s. 60(3)).",
        ["ssc2020-s60"],
    )
    if emp.children_count is not None and emp.children_count >= 2:
        _add_claim(
            ctx,
            "A woman with two or more surviving children is entitled to twelve weeks, of which not more than "
            "six weeks may precede the expected date of delivery (first proviso to s. 60(3)).",
            ["ssc2020-s60"],
        )
    _add_claim(
        ctx,
        "A woman entitled to maternity benefit is also entitled to a medical bonus of ₹3,500, or the amount "
        "notified by the Central Government, if the employer does not provide pre-natal confinement and "
        "post-natal care free of charge (s. 64).",
        ["ssc2020-s64"],
    )
    _add_claim(
        ctx,
        "On returning to duty, a woman is allowed two nursing breaks a day until the child attains fifteen "
        "months (s. 66).",
        ["ssc2020-s66"],
    )
    if request.establishment.employee_count is not None and request.establishment.employee_count >= 50:
        _add_claim(
            ctx,
            "An establishment with fifty or more employees must provide a crèche facility, and the woman is "
            "allowed four visits a day to the crèche (s. 67).",
            ["ssc2020-s67"],
        )
    if emp.event is SeparationEvent.TERMINATION or (request.question and "dismiss" in request.question.lower()):
        _add_claim(
            ctx,
            "It is unlawful for an employer to discharge or dismiss a woman during or on account of her "
            "absence under Chapter VI; an appeal lies to the competent authority within sixty days (s. 68).",
            ["ssc2020-s68"],
            kind="procedural",
        )


def _compose_harassment(ctx) -> None:
    risks = set(ctx.risks)
    _add_claim(
        ctx,
        "The POSH Act, 2013 applies to the workplace and is in force. Every employer must constitute an "
        "Internal Complaints Committee by an order in writing (s. 4).",
        ["posh2013-s4"],
    )
    _add_claim(
        ctx,
        "An aggrieved woman may complain in writing to the Internal Committee, or the Local Committee where "
        "none is constituted, within three months of the incident (or the last incident in a series); the "
        "Committee may extend the period by up to three months for reasons recorded in writing (s. 9).",
        ["posh2013-s9"],
        kind="procedural",
    )
    _add_claim(
        ctx,
        "The Internal Committee must inquire into the complaint, and the contents of the complaint and inquiry "
        "are confidential (ss. 11 and 16).",
        ["posh2013-s11", "posh2013-s16"],
        kind="procedural",
    )
    _add_claim(
        ctx,
        "Failure to constitute the Internal Committee, or to act under the Act, is punishable with a fine "
        "which may extend to ₹50,000; a repeat conviction attracts twice the punishment and possible "
        "cancellation or non-renewal of registration or licence (s. 26).",
        ["posh2013-s26"],
    )
    if "harassment_complaint" in risks:
        _add_claim(
            ctx,
            "The desk cannot determine whether the conduct alleged constitutes sexual harassment, decide the "
            "complaint, or recommend action against an individual. That is a contested factual and legal "
            "question for the Internal Committee and, where necessary, legal counsel.",
            ["posh2013-s4", "posh2013-s11"],
            kind="escalation",
        )


def _summary(ctx) -> str:
    domain = ctx.domain
    risks = set(ctx.risks)
    request = ctx.request
    emp = request.employment
    if ctx.verification is not None and ctx.verification.issues:
        first = ctx.verification.issues[0]
        return (
            "The draft could not be fully verified "
            f"({first.reason}). The request is escalated rather than answered with an unverified proposition."
        )
    if domain is Domain.GRATUITY:
        if ctx.as_of < SSC_COMMENCEMENT:
            return (
                "On the requested date the Payment of Gratuity Act, 1972 governs. The cited provisions state the "
                "five-year continuous-service rule and the death or disablement exception."
            )
        if ctx.coverage is not None and ctx.coverage.covered is False:
            return (
                "On the stated headcount the establishment is outside Chapter V (Gratuity), which applies to a "
                "shop or establishment with ten or more employees. The cited threshold is in the First Schedule."
            )
        if ctx.coverage is not None and ctx.coverage.covered is None:
            return (
                "Gratuity coverage cannot be confirmed until the peak headcount for the preceding twelve months is "
                "supplied; the threshold is ten employees. The substantive citations are set out below."
            )
        waived = emp.event in (SeparationEvent.DEATH, SeparationEvent.DISABLEMENT, SeparationEvent.EXPIRY)
        if waived and emp.worker_type is not None and emp.worker_type.value == "fixed_term":
            return (
                "Under s. 53(1) of the Code on Social Security, 2020, gratuity is payable on termination of a "
                "fixed term contract; the second proviso waives the five-year condition on expiry and the third "
                "proviso to s. 53(2) requires a pro rata payment. The 1972 Act is repealed and is background only."
            )
        if waived:
            return (
                "Under s. 53(1) and its second proviso, gratuity is payable without five years' continuous service "
                "where termination is due to death, disablement, or expiry of a fixed term contract. The 1972 Act "
                "is repealed and is background only."
            )
        if emp.completed_service_months is not None and emp.completed_service_months < 60:
            return (
                "On the stated service the five-year continuous-service threshold in s. 53(1) is not met, so "
                "gratuity is not payable unless a statutory exception applies."
            )
        return (
            "Gratuity is governed by s. 53 of the Code on Social Security, 2020: five years' continuous service is "
            "required at termination, the rate is fifteen days' wages per completed year under s. 53(2), and any "
            "forfeiture is confined to s. 53(6)."
        )
    if domain is Domain.MATERNITY_BENEFIT:
        if ctx.as_of < SSC_COMMENCEMENT:
            return (
                "The Code on Social Security, 2020 is not in force on the requested date and the corpus does not "
                "contain the then-applicable Maternity Benefit Act, 1961. The request is escalated."
            )
        return (
            "Maternity benefit under Chapter VI requires eighty days of actual work in the twelve months before "
            "the expected delivery and provides up to twenty-six weeks of benefit, with medical bonus and nursing "
            "breaks. The cited provisions state each condition."
        )
    if domain is Domain.WORKPLACE_HARASSMENT:
        if "harassment_complaint" in risks:
            return (
                "The request concerns a specific harassment complaint. The POSH Act requires an Internal "
                "Complaints Committee and a time-bound inquiry, but the desk cannot decide the complaint; it is "
                "escalated with the governing provisions."
            )
        return (
            "The POSH Act, 2013 requires every employer to constitute an Internal Complaints Committee, provides "
            "a three-month complaint window, and sets a fine of up to ₹50,000 for non-compliance."
        )
    return "The request falls outside the indexed corpus and is escalated without a legal conclusion."


# ----------------------------------------------------------------------- routing
def decide_route(ctx) -> RouteDecision:
    verification = ctx.verification
    issues = verification.issues if verification else []
    risks = set(ctx.risks)
    if ctx.domain is Domain.OTHER:
        return RouteDecision(
            route=Route.LEGAL_COUNSEL_REVIEW,
            basis="No indexed provision addresses the request.",
            confidence=0.3,
            escalation_path=["compliance_desk", "employment_counsel"],
            next_actions=["Route the request to employment counsel; do not answer from an unrelated provision."],
        )
    if "harassment_complaint" in risks:
        return RouteDecision(
            route=Route.LEGAL_COUNSEL_REVIEW,
            basis="The request asks the desk to decide or act on a specific harassment complaint.",
            confidence=0.82,
            escalation_path=["internal_committee", "employment_counsel"],
            required_fields=[],
            next_actions=[
                "Refer the complaint to the Internal Complaints Committee constituted under s. 4 of the POSH Act.",
                "Preserve confidentiality under s. 16 and follow the inquiry procedure in s. 11.",
                "Involve employment counsel before any adverse action against any person.",
            ],
        )
    if "termination_dispute" in risks:
        return RouteDecision(
            route=Route.LEGAL_COUNSEL_REVIEW,
            basis="The request raises a contested termination question that the desk is not authorised to decide.",
            confidence=0.78,
            escalation_path=["compliance_desk", "employment_counsel"],
            next_actions=["Escalate with the service record, notices, and the cited provisions."],
        )
    if verification is not None and verification.outcome.value == "failed":
        return RouteDecision(
            route=Route.LEGAL_COUNSEL_REVIEW,
            basis="Verification did not pass: " + "; ".join(issue.reason for issue in issues[:3]),
            confidence=0.35,
            escalation_path=["compliance_desk", "employment_counsel"],
            next_actions=["Do not publish the unverified draft; route it to counsel with the trace."],
        )
    if ctx.coverage is not None and ctx.coverage.covered is None:
        return RouteDecision(
            route=Route.REQUEST_MORE_ESTABLISHMENT_DETAIL,
            basis=ctx.coverage.reason,
            confidence=0.62,
            required_fields=["establishment.employee_count"],
            next_actions=[
                "Obtain the peak headcount for any day of the preceding twelve months.",
                "Re-run the advisory with the headcount so the coverage threshold can be applied.",
            ],
        )
    next_actions = _next_actions(ctx)
    return RouteDecision(
        route=Route.ANSWER_WITH_CITATION,
        basis="Every operative claim cites a provision that is in force on the requested date.",
        confidence=0.9,
        next_actions=next_actions,
    )


def _next_actions(ctx) -> list[str]:
    if ctx.domain is Domain.GRATUITY:
        actions = [
            "Record the last drawn wages and the service record used in the calculation.",
            "Apply s. 53(6) before releasing payment if any damage, loss, or misconduct issue is alleged.",
        ]
        if ctx.request.employment.completed_service_months is None:
            actions.append("Confirm continuous service from the appointment and separation dates or the 240-day test in s. 54.")
        return actions
    if ctx.domain is Domain.MATERNITY_BENEFIT:
        return [
            "Verify the days actually worked in the twelve months before the expected delivery date.",
            "Record the expected delivery date and the number of surviving children to fix the duration.",
            "Check whether the establishment has fifty or more employees for the crèche obligation.",
        ]
    if ctx.domain is Domain.WORKPLACE_HARASSMENT:
        return [
            "Constitute or reconstitute the Internal Complaints Committee under s. 4 and publish its members.",
            "Record complaints and the three-month limitation, including any reasoned extension under s. 9.",
            "Maintain confidentiality under s. 16 and submit the annual report under s. 21.",
        ]
    return ["Route the request to employment counsel."]


__all__ = [
    "DraftData",
    "QuerySpec",
    "classify",
    "compose",
    "decide_route",
    "qualifier_provisions",
    "query_plan",
    "required_provisions",
]
