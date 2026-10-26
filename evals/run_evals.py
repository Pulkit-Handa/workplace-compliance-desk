#!/usr/bin/env python3
"""Golden-set evaluation for the Workplace Compliance Advisory Desk.

The evaluation runs the real LangGraph pipeline in-process (no API, no Redis)
against the checked-in golden set, for the baseline and the qualifier-aware
variant.  It writes a machine-readable report and a reviewer-friendly markdown
summary.

Metrics (definitions are repeated in docs/evals.md):

groundedness           supported claims / claims checked.  A claim is supported
                       only when deterministic verification finds a retrieved,
                       in-force citation and lexical support.
context_relevance       relevant retrieved passages / all retrieved passages.
                       "Relevant" uses the golden case's provision set, so a
                       passage from an unrelated statute is a miss even if it is
                       topically adjacent.
context_recall          required provisions retrieved in the run / required
                       provisions.  It isolates retrieval coverage from the
                       final citation list.
faithfulness            mean passage-support score of cited, non-escalation
                       claims.  It measures how much of each claim's wording is
                       actually present in its citations.
hallucination_rate     unsupported claims / claims checked.  It is a subset of
                       (1 - groundedness) because a run can fail verification on
                       a missing qualifier while every individual claim is still
                       supported.
citation_precision     cited provisions in the relevant set / all cited provisions.
citation_recall        required provisions cited / required provisions.
qualifier_coverage     required qualifier provisions cited / required qualifiers.
route_accuracy         actual route equals the expected route.
verification_accuracy  actual verification outcome equals the expectation.

Usage:

    PYTHONPATH=src python3 evals/run_evals.py --variant both
    PYTHONPATH=src python3 -m compliance.cli eval --variant both
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from compliance.agents.graph import run_advisory
from compliance.config import settings
from compliance.contracts import ComplianceRequest
from compliance.corpus.index import get_default_index
from compliance.llm.prompts import PromptRegistry

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GOLDEN = REPO_ROOT / "evals" / "golden.jsonl"
DEFAULT_OUT = REPO_ROOT / "reports" / "eval_report.json"
DEFAULT_MATRIX = REPO_ROOT / "reports" / "variant_matrix.json"


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return ordered[index]


def _mean(values: list[float]) -> float:
    return round(statistics.fmean(values), 4) if values else 0.0


def _ratio(numerator: int, denominator: int, *, empty: float = 1.0) -> float:
    if denominator == 0:
        return empty
    return round(numerator / denominator, 4)


def load_golden(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            case = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
        for key in (
            "id",
            "request",
            "expected_route",
            "expected_verification",
            "required_provisions",
            "relevant_provisions",
        ):
            if key not in case:
                raise ValueError(f"{path}:{line_number}: missing {key!r}")
        cases.append(case)
    if not cases:
        raise ValueError(f"{path}: no golden cases")
    return cases


@dataclass
class CaseRun:
    case_id: str
    variant: str
    actual_route: str
    actual_verification: str
    status: str
    latency_ms: float
    claims: int
    claims_supported: int
    citation_ids: list[str]
    retrieved_ids: list[str]
    evidence_count: int
    issues: list[dict[str, Any]] = field(default_factory=list)
    degraded: bool = False
    loops: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "actual_route": self.actual_route,
            "actual_verification": self.actual_verification,
            "status": self.status,
            "latency_ms": round(self.latency_ms, 2),
            "claims": self.claims,
            "claims_supported": self.claims_supported,
            "citation_ids": self.citation_ids,
            "retrieved_ids": self.retrieved_ids,
            "evidence_count": self.evidence_count,
            "issues": self.issues,
            "degraded": self.degraded,
            "loops": self.loops,
        }


async def run_case(case: dict[str, Any], variant: str) -> CaseRun:
    request = ComplianceRequest.model_validate(case["request"])
    evidence: list[dict[str, Any]] = []
    draft: dict[str, Any] = {}
    result = await run_advisory(
        request,
        variant=variant,
        evidence_sink=evidence,
        draft_sink=draft,
    )
    claims = draft.get("claims", result.claims)
    citations = draft.get("citations", result.citations)
    citation_ids = [citation.id.split("::", 1)[0] for citation in citations.values()]
    retrieved_ids = [str(item.get("provision_id")) for item in evidence]
    return CaseRun(
        case_id=case["id"],
        variant=variant,
        actual_route=result.route.value,
        actual_verification=result.verification.outcome.value,
        status=result.status.value,
        latency_ms=result.latency_ms,
        claims=len(claims),
        claims_supported=result.verification.claims_supported,
        citation_ids=citation_ids,
        retrieved_ids=retrieved_ids,
        evidence_count=len(evidence),
        issues=[issue.model_dump(mode="json") for issue in result.verification.issues],
        degraded=result.degraded,
        loops=result.verification.loops,
    )


def score_case(case: dict[str, Any], run: CaseRun, claims: list[Any]) -> dict[str, Any]:
    required = set(case.get("required_provisions") or [])
    relevant = set(case.get("relevant_provisions") or [])
    qualifiers = set(case.get("qualifier_provisions") or [])
    cited = set(run.citation_ids)
    retrieved = run.retrieved_ids

    supported = sum(1 for claim in claims if claim.supported)
    claims_checked = len(claims)
    hallucinated = claims_checked - supported

    factual_claims = [
        claim
        for claim in claims
        if claim.kind != "escalation" and claim.citations
    ]
    faithfulness = (
        statistics.fmean(claim.support_score for claim in factual_claims)
        if factual_claims
        else 1.0
    )

    cited_relevant = len(cited & relevant)
    required_cited = len(required & cited)
    qualifiers_cited = len(qualifiers & cited)
    relevant_retrieved = sum(1 for item in retrieved if item in relevant)

    route_match = run.actual_route == case["expected_route"]
    verification_match = run.actual_verification == case["expected_verification"]
    citation_recall = _ratio(required_cited, len(required))
    qualifier_coverage = _ratio(qualifiers_cited, len(qualifiers))
    hallucination_rate = _ratio(hallucinated, claims_checked, empty=0.0)

    success = (
        route_match
        and verification_match
        and citation_recall >= 1.0
        and qualifier_coverage >= 1.0
        and hallucination_rate == 0.0
    )

    return {
        "success": success,
        "route_match": route_match,
        "verification_match": verification_match,
        "groundedness": _ratio(supported, claims_checked, empty=1.0),
        "context_relevance": _ratio(relevant_retrieved, len(retrieved), empty=0.0),
        "context_recall": _ratio(
            sum(1 for item in required if item in set(retrieved)), len(required), empty=1.0
        ),
        "faithfulness": round(faithfulness, 4),
        "hallucination_rate": hallucination_rate,
        "citation_precision": _ratio(cited_relevant, len(cited), empty=1.0),
        "citation_recall": citation_recall,
        "qualifier_coverage": qualifier_coverage,
        "expected_provisions_missing": sorted(required - cited),
        "unexpected_citations": sorted(cited - relevant),
        "irrelevant_retrieved": sorted(set(retrieved) - relevant),
        "malformed_retrieval_ids": sum(1 for item in retrieved if item in {"", "None"}),
    }


def summarise(runs: list[dict[str, Any]]) -> dict[str, Any]:
    if not runs:
        return {}
    latencies = [run["metrics"]["latency_ms"] for run in runs]
    quality_keys = (
        "groundedness",
        "context_relevance",
        "context_recall",
        "faithfulness",
        "hallucination_rate",
        "citation_precision",
        "citation_recall",
        "qualifier_coverage",
    )
    summary = {
        "cases": len(runs),
        "success_rate": _ratio(sum(run["metrics"]["success"] for run in runs), len(runs), empty=0.0),
        "route_accuracy": _ratio(sum(run["metrics"]["route_match"] for run in runs), len(runs), empty=0.0),
        "verification_accuracy": _ratio(
            sum(run["metrics"]["verification_match"] for run in runs), len(runs), empty=0.0
        ),
        "answers_with_citation": sum(
            1 for run in runs if run["actual"]["actual_route"] == "answer_with_citation"
        ),
        "escalations": sum(
            1 for run in runs if run["actual"]["actual_route"] == "legal_counsel_review"
        ),
        "degraded_runs": sum(1 for run in runs if run["actual"]["degraded"]),
        "latency_p50_ms": round(statistics.median(latencies), 2),
        "latency_p95_ms": round(_p95(latencies), 2),
        "latency_max_ms": round(max(latencies), 2),
    }
    for key in quality_keys:
        summary[key] = _mean([run["metrics"][key] for run in runs])
    return summary


async def evaluate(golden_path: Path, variants: list[str]) -> dict[str, Any]:
    cases = load_golden(golden_path)
    index = get_default_index()
    started = time.perf_counter()
    records: list[dict[str, Any]] = []
    per_variant: dict[str, list[dict[str, Any]]] = {variant: [] for variant in variants}

    for case in cases:
        case_record: dict[str, Any] = {
            "id": case["id"],
            "title": case.get("title", case["id"]),
            "category": case.get("category", "uncategorised"),
            "expected": {
                "route": case["expected_route"],
                "verification": case["expected_verification"],
                "required_provisions": case.get("required_provisions", []),
                "qualifier_provisions": case.get("qualifier_provisions", []),
                "relevant_provisions": case.get("relevant_provisions", []),
            },
            "variants": {},
        }
        # The full result is needed to score claim-level support; keep the runs
        # sequential so the in-process metrics registry and memory stay bounded.
        for variant in variants:
            request = ComplianceRequest.model_validate(case["request"])
            evidence: list[dict[str, Any]] = []
            draft: dict[str, Any] = {}
            result = await run_advisory(
                request, variant=variant, evidence_sink=evidence, draft_sink=draft
            )
            claims = draft.get("claims", result.claims)
            citations = draft.get("citations", result.citations)
            run = CaseRun(
                case_id=case["id"],
                variant=variant,
                actual_route=result.route.value,
                actual_verification=result.verification.outcome.value,
                status=result.status.value,
                latency_ms=result.latency_ms,
                claims=len(claims),
                claims_supported=result.verification.claims_supported,
                citation_ids=[c.id.split("::", 1)[0] for c in citations.values()],
                retrieved_ids=[str(item.get("provision_id")) for item in evidence],
                evidence_count=len(evidence),
                issues=[i.model_dump(mode="json") for i in result.verification.issues],
                degraded=result.degraded,
                loops=result.verification.loops,
            )
            metrics = score_case(case, run, claims)
            metrics["latency_ms"] = round(result.latency_ms, 2)
            metrics["claims"] = len(claims)
            metrics["claims_supported"] = result.verification.claims_supported
            metrics["published_claims"] = len(result.claims)
            metrics["evidence_count"] = len(evidence)
            metrics["citation_count"] = len(citations)
            metrics["issue_reasons"] = sorted({issue.reason for issue in result.verification.issues})
            case_record["variants"][variant] = {
                "actual": run.as_dict(),
                "metrics": metrics,
                "answer_excerpt": result.advisory[:1200],
                "route_basis": result.route_decision.basis,
            }
            per_variant[variant].append(
                {
                    "case_id": case["id"],
                    "actual": run.as_dict(),
                    "metrics": metrics,
                }
            )
        records.append(case_record)

    elapsed = time.perf_counter() - started
    current = {}
    current_path = Path(settings.index_dir) / "current.json"
    if current_path.exists():
        current = json.loads(current_path.read_text(encoding="utf-8"))

    report: dict[str, Any] = {
        "meta": {
            "generated_at": datetime.now(UTC).isoformat(),
            "golden_set": str(golden_path),
            "cases": len(cases),
            "variants": variants,
            "index_version": index.index_version,
            "corpus_hash": current.get("corpus_hash"),
            "embedding_provider": settings.embedding_provider,
            "llm_provider": settings.llm_provider,
            "evaluation_seconds": round(elapsed, 3),
            "execution": "in-process LangGraph pipeline; deterministic local provider",
            "metric_definitions": {
                "groundedness": "supported claims / claims checked",
                "context_relevance": "relevant retrieved passages / all retrieved passages",
                "context_recall": "required provisions retrieved in this run / required provisions",
                "faithfulness": "mean passage-support score of cited non-escalation claims",
                "hallucination_rate": "unsupported claims / claims checked",
                "citation_precision": "cited provisions in the relevant set / all cited provisions",
                "citation_recall": "required provisions cited / required provisions",
                "qualifier_coverage": "required qualifier provisions cited / required qualifiers",
                "route_accuracy": "actual route equals the expected route",
                "verification_accuracy": "actual verification equals the expected verification",
            },
            "prompt_versions": {
                variant: PromptRegistry(variant=variant).versions(variant) for variant in variants
            },
        },
        "summary": {variant: summarise(per_variant[variant]) for variant in variants},
        "cases": records,
    }

    if len(variants) > 1:
        baseline, candidate = variants[0], variants[-1]
        base_summary = report["summary"][baseline]
        cand_summary = report["summary"][candidate]
        report["comparison"] = {
            "baseline": baseline,
            "candidate": candidate,
            "delta": {
                key: round(cand_summary.get(key, 0) - base_summary.get(key, 0), 4)
                for key in (
                    "success_rate",
                    "route_accuracy",
                    "verification_accuracy",
                    "groundedness",
                    "context_relevance",
                    "context_recall",
                    "citation_recall",
                    "qualifier_coverage",
                    "latency_p95_ms",
                )
            },
            "escalation_delta": cand_summary.get("escalations", 0) - base_summary.get("escalations", 0),
            "interpretation": (
                f"{candidate} answers more cases with citations because it runs qualifier-aware "
                f"retrieval and the verification loop; {baseline} is the single-pass baseline."
            ),
        }
    return report


def _pct(value: float) -> str:
    return f"{value * 100:.0f}%"


def render_markdown(report: dict[str, Any]) -> str:
    meta = report["meta"]
    lines: list[str] = []
    lines.append("# Golden-set evaluation — Workplace Compliance Advisory Desk")
    lines.append("")
    lines.append(f"Generated **{meta['generated_at']}** · {meta['cases']} synthetic cases · "
                 f"index `{meta['index_version']}` · provider `{meta['llm_provider']}`")
    lines.append("")
    lines.append("The evaluation runs the production LangGraph pipeline in-process against the checked-in "
                 "golden set. It is not a load test; the SLO measurement is in `reports/load_report.md`.")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | " + " | ".join(meta["variants"]) + " |")
    lines.append("|---|" + "---:|" * len(meta["variants"]))
    summary = report["summary"]
    rows = [
        ("Cases", "cases", "int"),
        ("Success rate", "success_rate", "pct"),
        ("Route accuracy", "route_accuracy", "pct"),
        ("Verification accuracy", "verification_accuracy", "pct"),
        ("Groundedness", "groundedness", "pct"),
        ("Context relevance", "context_relevance", "pct"),
        ("Context recall", "context_recall", "pct"),
        ("Faithfulness", "faithfulness", "pct"),
        ("Hallucination rate", "hallucination_rate", "pct"),
        ("Citation precision", "citation_precision", "pct"),
        ("Citation recall", "citation_recall", "pct"),
        ("Qualifier coverage", "qualifier_coverage", "pct"),
        ("Answers with citation", "answers_with_citation", "int"),
        ("Escalations", "escalations", "int"),
        ("Degraded runs", "degraded_runs", "int"),
        ("p50 latency (ms)", "latency_p50_ms", "num"),
        ("p95 latency (ms)", "latency_p95_ms", "num"),
    ]
    for label, key, kind in rows:
        cells = []
        for variant in meta["variants"]:
            value = summary[variant].get(key, 0)
            if kind == "pct":
                cells.append(_pct(value))
            elif kind == "int":
                cells.append(str(value))
            else:
                cells.append(f"{value:.1f}")
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    lines.append("")

    if "comparison" in report:
        comp = report["comparison"]
        delta = comp["delta"]
        lines.append("## Variant comparison")
        lines.append("")
        lines.append(f"`{comp['candidate']}` versus `{comp['baseline']}`:")
        lines.append("")
        lines.append(f"- Success rate: **{delta['success_rate'] * 100:+.0f} points**")
        lines.append(f"- Verification accuracy: **{delta['verification_accuracy'] * 100:+.0f} points**")
        lines.append(f"- Citation recall: **{delta['citation_recall'] * 100:+.0f} points**")
        lines.append(f"- Context recall: **{delta['context_recall'] * 100:+.0f} points**")
        lines.append(f"- Qualifier coverage: **{delta['qualifier_coverage'] * 100:+.0f} points**")
        lines.append(f"- Escalations: **{comp['escalation_delta']:+d}**")
        lines.append("")
        lines.append(comp["interpretation"])
        lines.append("")

    lines.append("## Per-case results")
    lines.append("")
    header = "| Case | Expected route | " + " | ".join(
        f"{variant} route / verification / recall" for variant in meta["variants"]
    ) + " |"
    lines.append(header)
    lines.append("|---|---|" + "---|" * len(meta["variants"]))
    for case in report["cases"]:
        cells = []
        for variant in meta["variants"]:
            item = case["variants"][variant]
            actual = item["actual"]
            metrics = item["metrics"]
            mark = "PASS" if metrics["success"] else "FAIL"
            cells.append(
                f"{actual['actual_route']} / {actual['actual_verification']} / "
                f"{metrics['citation_recall']:.0%} ({mark})"
            )
        lines.append(f"| `{case['id']}` | {case['expected']['route']} | " + " | ".join(cells) + " |")
    lines.append("")

    lines.append("## Notes")
    lines.append("")
    lines.append("- Groundedness and hallucination are claim-level checks. A run can fail verification on a "
                 "missing qualifier while every individual claim remains grounded; that is why the two "
                 "outcomes are reported separately.")
    lines.append("- Context relevance is computed from the passages actually retrieved during the run, not "
                 "from the final citation list. Unused but relevant passages count as relevant. The "
                 "qualifier-aware variant deliberately retrieves more related material, so its precision can "
                 "be lower even while its context recall is higher; both numbers are reported.")
    lines.append("- Context recall uses the required-provision set. It isolates whether the decisive proviso, "
                 "definition, or threshold reached the agent at all.")
    lines.append("- The `outside` case expects zero relevant passages and no citation. An escalation is the "
                 "correct outcome; answering it from the gratuity corpus would be a failure.")
    lines.append("- The 1972 Act is intentionally present in the index as repealed. The evaluation checks that "
                 "operative claims under the Code do not use it as authority, while the historical case does.")
    lines.append("")
    return "\n".join(lines)


def write_variant_matrix(report: dict[str, Any], path: Path) -> None:
    matrix: list[dict[str, Any]] = []
    for case in report["cases"]:
        row: dict[str, Any] = {
            "id": case["id"],
            "category": case["category"],
            "expected": case["expected"],
        }
        for variant, item in case["variants"].items():
            row[variant] = {
                "route": item["actual"]["actual_route"],
                "verification": item["actual"]["actual_verification"],
                "status": item["actual"]["status"],
                "claims": item["actual"]["claims"],
                "claims_supported": item["actual"]["claims_supported"],
                "citation_recall": item["metrics"]["citation_recall"],
                "qualifier_coverage": item["metrics"]["qualifier_coverage"],
                "issue_reasons": item["metrics"]["issue_reasons"],
                "latency_ms": item["actual"]["latency_ms"],
            }
        matrix.append(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"generated_at": report["meta"]["generated_at"], "cases": matrix}, indent=2), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the compliance advisory golden-set evaluation")
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    parser.add_argument("--variant", choices=["v1", "v2", "both"], default="both")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    parser.add_argument("--json-only", action="store_true", help="skip the markdown report")
    args = parser.parse_args(argv)

    variants = ["v1", "v2"] if args.variant == "both" else [args.variant]
    report = asyncio.run(evaluate(args.golden, variants))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    if not args.json_only:
        markdown_path = args.out.with_suffix(".md")
        markdown_path.write_text(render_markdown(report), encoding="utf-8")
    write_variant_matrix(report, args.matrix)

    for variant in variants:
        summary = report["summary"][variant]
        print(
            f"{variant}: success={summary['success_rate']:.0%} "
            f"verification={summary['verification_accuracy']:.0%} "
            f"recall={summary['citation_recall']:.0%} "
            f"escalations={summary['escalations']}/{summary['cases']} "
            f"p95={summary['latency_p95_ms']:.0f}ms"
        )
    print(f"wrote {args.out}")
    if not args.json_only:
        print(f"wrote {args.out.with_suffix('.md')}")
    print(f"wrote {args.matrix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
