# Evaluation

## Purpose

The golden set answers a narrow question: **does the current pipeline produce the right route, the right verification outcome, the required citations, and the fact-pattern qualifiers, for a fixed set of synthetic cases?**

It is not a load test, a benchmark against a frontier model, or proof of legal correctness. It is a regression gate for the properties this project claims.

## Reproducing

```bash
PYTHONPATH=src python3 evals/run_evals.py --variant both
```

or through the CLI:

```bash
python3 -m compliance.cli eval --variant both
```

Inputs:

- `evals/golden.jsonl` — 24 synthetic cases.
- The current index (`GET /v1/index` reports the version).
- `stub_local` by default; no network.

Outputs:

- `reports/eval_report.json` — machine-readable, per-case and aggregate.
- `reports/eval_report.md` — reviewer summary.
- `reports/variant_matrix.json` — compact v1/v2 comparison.

The evaluation runs the **real LangGraph pipeline in-process**, not a mock of the agents. It does not start Redis, the API, or the worker. The production path is covered separately by the API and Compose smoke tests.

## Golden cases

| Case | Category | What it tests |
|---|---|---|
| `fixed_expiry` | repealed-act boundary | Fixed-term expiry after three years, five-year waiver, pro-rata rate, repeal of the 1972 Act |
| `fixed_expiry_18m` | hard case (section 4.1) | Written 18-month fixed term engaged after commencement and completed; waiver and pro-rata rate |
| `fixed_term_running` | lifecycle | Fixed-term employee still in service; when the obligation arises |
| `resignation_7y` | entitlement | Ordinary five-year entitlement and calculation |
| `resignation_5y_exact` | threshold | Exactly five years; boundary is inclusive |
| `resignation_3y` | threshold | Three years, no waiver; no entitlement and no misleading arithmetic |
| `resignation_59m` | threshold | One month short of five years; no entitlement |
| `death` | waiver | Death after two years; nominee entitlement and five-year waiver |
| `death_1y` | waiver | Death after twelve months; waiver applies |
| `disablement_4y` | waiver | Disablement after four years; waiver applies |
| `coverage_unknown` | applicability | Seven-person startup; coverage threshold must be addressed |
| `maternity` | maternity benefit | 100 days, two children, duration and coverage |
| `maternity_80_days` | maternity eligibility | Exactly 80 days; eligibility boundary |
| `maternity_creche_50` | maternity ancillary | 100 days, two children, and the s. 67 crèche duty at fifty employees |
| `historical` | as-of retrieval | 1 June 2024; the 1972 Act is the regime in force |
| `historical_death_2019` | as-of + waiver | 1 June 2019 death after two years under the 1972 Act |
| `historical_below_5y` | as-of + threshold | 1 January 2020 resignation after three years; no entitlement |
| `posh_policy` | POSH | ICC constitution, complaint window, fine |
| `posh_committee_20` | POSH | ICC at a twenty-person workplace |
| `posh_timeline` | POSH | Three-month deadline and extension |
| `posh_complaint` | escalation | Specific harassment allegation — must not decide it |
| `termination_dispute` | escalation | Contested termination advice — must not decide it |
| `outside` | out of scope | Minimum wage — not in corpus, must escalate |
| `pf_question` | out of scope | Provident fund — not in corpus, must escalate |

The expected route, verification outcome, required provisions, qualifier provisions, and relevant provisions are all checked in. A change to the legal policy should be accompanied by a change to the case expectations, reviewed as a legal-content change.

## Metric definitions

| Metric | Definition |
|---|---|
| **Success** | route match **and** verification match **and** citation recall = 100% **and** qualifier coverage = 100% **and** hallucination rate = 0 |
| **Route accuracy** | actual route equals expected route |
| **Verification accuracy** | actual verification outcome equals expected outcome |
| **Groundedness** | supported claims / claims checked |
| **Context relevance** | relevant retrieved passages / all retrieved passages |
| **Context recall** | required provisions retrieved in the run / required provisions |
| **Faithfulness** | mean passage-support score of cited, non-escalation claims |
| **Hallucination rate** | unsupported claims / claims checked |
| **Citation precision** | cited provisions in the relevant set / all cited provisions |
| **Citation recall** | required provisions cited / required provisions |
| **Qualifier coverage** | required qualifier provisions cited / required qualifiers |
| **Answers with citation** | runs routed `answer_with_citation` |
| **Escalations** | runs routed `legal_counsel_review` |
| **Degraded runs** | runs that took a degraded path (provider/tool fallback) |

Two subtleties:

- Groundedness and hallucination are claim-level. A run can fail verification because a **qualifier is missing** while every claim it did make is grounded. The report keeps these separate.
- Context relevance counts passages actually retrieved, not only cited. v2 deliberately retrieves more related material, so its context precision can be lower while its recall is higher. Both are reported.

## Results

Generated 2026-10-26, index `idx-1793004115-b6107331`, provider `stub_local`:

| Metric | v1 | v2 |
|---|---:|---:|
| Cases | 24 | 24 |
| Success rate | 12% | **100%** |
| Route accuracy | 21% | **100%** |
| Verification accuracy | 17% | **100%** |
| Groundedness | 96% | **100%** |
| Context relevance | 55% | 61% |
| Context recall | 52% | **100%** |
| Faithfulness | 79% | 79% |
| Hallucination rate | 4% | **0%** |
| Citation precision | 100% | 100% |
| Citation recall | 52% | **100%** |
| Qualifier coverage | 40% | **100%** |
| Answers with citation | 1 | 20 |
| Escalations | 23 | 4 |
| Degraded runs | 0 | 0 |
| p50 latency | 13.0 ms | 27.2 ms |
| p95 latency | 18.1 ms | 37.3 ms |

### Per-case

| Case | Expected | v1 route / verification / recall | v2 route / verification / recall |
|---|---|---|---|
| `fixed_expiry` | answer | counsel / failed / 67% | answer / passed / 100% |
| `fixed_expiry_18m` | answer | counsel / failed / 60% | answer / passed / 100% |
| `fixed_term_running` | answer | counsel / failed / 67% | answer / passed / 100% |
| `resignation_7y` | answer | counsel / failed / 33% | answer / passed / 100% |
| `resignation_5y_exact` | answer | counsel / failed / 33% | answer / passed / 100% |
| `resignation_3y` | answer | counsel / failed / 50% | answer / passed / 100% |
| `resignation_59m` | answer | counsel / failed / 50% | answer / passed / 100% |
| `death` | answer | counsel / failed / 75% | answer / passed / 100% |
| `death_1y` | answer | counsel / failed / 33% | answer / passed / 100% |
| `disablement_4y` | answer | counsel / failed / 33% | answer / passed / 100% |
| `coverage_unknown` | answer | counsel / failed / 0% | answer / passed / 100% |
| `maternity` | answer | counsel / failed / 50% | answer / passed / 100% |
| `maternity_80_days` | answer | counsel / failed / 50% | answer / passed / 100% |
| `maternity_creche_50` | answer | counsel / failed / 67% | answer / passed / 100% |
| `historical` | answer | counsel / failed / 50% | answer / passed / 100% |
| `historical_death_2019` | answer | counsel / failed / 50% | answer / passed / 100% |
| `historical_below_5y` | answer | answer / passed / 50% | answer / passed / 100% |
| `posh_policy` | answer | counsel / failed / 67% | answer / passed / 100% |
| `posh_committee_20` | answer | counsel / failed / 0% | answer / passed / 100% |
| `posh_timeline` | answer | counsel / failed / 33% | answer / passed / 100% |
| `posh_complaint` | counsel | counsel / failed / 33% | counsel / passed / 100% |
| `termination_dispute` | counsel | counsel / passed / 100% | counsel / passed / 100% |
| `outside` | counsel | counsel / passed / 100% | counsel / passed / 100% |
| `pf_question` | counsel | counsel / passed / 100% | counsel / passed / 100% |

v2's four escalations are **correct**: a specific harassment complaint, a contested
termination request, an out-of-corpus minimum-wage question, and an out-of-corpus
provident-fund question. The target is not “answer everything”; it is “answer what is
supported and escalate the rest”.

## What the variants do

- **v1** — one retrieval query, top-k 6, no curated provision fetch, no related expansion, no retry. It is the control.
- **v2** — qualifier-aware multi-query plan, required-provision fetch, related expansion, and up to two verification-driven retries.

The jump from 12% to 100% is not a model upgrade. Both variants use the same `stub_local` provider and the same legal composer. v2 wins because it retrieves the **decisive provisos** and thresholds, and the verifier refuses a draft that omits them.

The two variants have essentially the same mean faithfulness (79% each). v2 makes more claims, including longer explanatory ones, so lexical support can stay flat even as citation recall and qualifier coverage reach 100%. Faithfulness is not the same as correctness, and it is not optimised in isolation.

## Why v1 fails

The failure is systematic, not random:

- The main section is retrieved; the proviso is not.
- The verifier marks the qualifier missing.
- The draft is withheld and the route becomes `legal_counsel_review`.
- The `outside` case is the exception: with no relevant corpus, escalating is correct, so v1 “passes” that one.

This is exactly the failure mode the project was built to catch. A single-pass RAG system can quote the right section and still give the wrong answer because it missed the exception.

## Evaluation limitations

- **24 cases is still small.** It is a regression and coverage set, not a statistically meaningful benchmark. It is intentionally readable and reviewable. The cases cover thresholds, waivers, coverage, the POSH boundary, historical as-of branches, and two escalation/out-of-scope regions.
- **Evaluation sees the private draft.** When verification fails, the public result withholds the draft's claims and citations. The evaluator uses a private draft sink to score the retrieval and verifier failure that caused the escalation, so groundedness/faithfulness and citation coverage remain diagnosable. A reviewer inspecting the API sees the withheld escalation, not those draft citations.
- **The provider is a stub.** The evaluation measures the pipeline and retrieval/verification design, not a real model's legal reasoning.
- **The embedder is lexical.** Context relevance is affected by the hashing embedder; sentence-transformers may change retrieval metrics.
- **Gold expectations are authored.** Required and relevant provision sets reflect the project's legal policy. A dispute about the law is a dispute about the fixture, not something the metric can settle.
- **In-process execution.** The evaluation excludes queueing, Redis, and network latency; the load test measures those.
- **No human legal review is bundled.** The reports support review; they do not replace it.

## Regression policy

When changing prompts, retrieval, policy, or corpus:

1. Run `pytest` (including the golden-matrix tests).
2. Run `evals/run_evals.py --variant both`.
3. Diff `reports/eval_report.md` and `reports/variant_matrix.json`.
4. Investigate any drop in route accuracy, verification accuracy, citation recall, or qualifier coverage.
5. If a legal expectation changes, update `evals/golden.jsonl` and explain the change in the change description.

The load test is separate: it validates the SLO on a topology, not legal quality.
