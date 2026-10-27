# Golden-set evaluation — Workplace Compliance Advisory Desk

Generated **2026-10-26T15:30:17.085696+00:00** · 24 synthetic cases · index `idx-1793004115-b6107331` · provider `stub_local`

The evaluation runs the production LangGraph pipeline in-process against the checked-in golden set. It is not a load test; the SLO measurement is in `reports/load_report.md`.

## Summary

| Metric | v1 | v2 |
|---|---:|---:|
| Cases | 24 | 24 |
| Success rate | 12% | 100% |
| Route accuracy | 21% | 100% |
| Verification accuracy | 17% | 100% |
| Groundedness | 96% | 100% |
| Context relevance | 55% | 61% |
| Context recall | 52% | 100% |
| Faithfulness | 79% | 79% |
| Hallucination rate | 4% | 0% |
| Citation precision | 100% | 100% |
| Citation recall | 52% | 100% |
| Qualifier coverage | 40% | 100% |
| Answers with citation | 1 | 20 |
| Escalations | 23 | 4 |
| Degraded runs | 0 | 0 |
| p50 latency (ms) | 13.0 | 27.2 |
| p95 latency (ms) | 18.1 | 37.3 |

## Variant comparison

`v2` versus `v1`:

- Success rate: **+88 points**
- Verification accuracy: **+83 points**
- Citation recall: **+48 points**
- Context recall: **+48 points**
- Qualifier coverage: **+60 points**
- Escalations: **-19**

v2 answers more cases with citations because it runs qualifier-aware retrieval and the verification loop; v1 is the single-pass baseline.

## Per-case results

| Case | Expected route | v1 route / verification / recall | v2 route / verification / recall |
|---|---|---|---|
| `fixed_expiry` | answer_with_citation | legal_counsel_review / failed / 67% (FAIL) | answer_with_citation / passed / 100% (PASS) |
| `resignation_7y` | answer_with_citation | legal_counsel_review / failed / 33% (FAIL) | answer_with_citation / passed / 100% (PASS) |
| `resignation_3y` | answer_with_citation | legal_counsel_review / failed / 50% (FAIL) | answer_with_citation / passed / 100% (PASS) |
| `death` | answer_with_citation | legal_counsel_review / failed / 75% (FAIL) | answer_with_citation / passed / 100% (PASS) |
| `coverage_unknown` | answer_with_citation | legal_counsel_review / failed / 0% (FAIL) | answer_with_citation / passed / 100% (PASS) |
| `maternity` | answer_with_citation | legal_counsel_review / failed / 50% (FAIL) | answer_with_citation / passed / 100% (PASS) |
| `historical` | answer_with_citation | legal_counsel_review / failed / 50% (FAIL) | answer_with_citation / passed / 100% (PASS) |
| `posh_policy` | answer_with_citation | legal_counsel_review / failed / 67% (FAIL) | answer_with_citation / passed / 100% (PASS) |
| `posh_complaint` | legal_counsel_review | legal_counsel_review / failed / 33% (FAIL) | legal_counsel_review / passed / 100% (PASS) |
| `outside` | legal_counsel_review | legal_counsel_review / passed / 100% (PASS) | legal_counsel_review / passed / 100% (PASS) |
| `fixed_expiry_18m` | answer_with_citation | legal_counsel_review / failed / 60% (FAIL) | answer_with_citation / passed / 100% (PASS) |
| `fixed_term_running` | answer_with_citation | legal_counsel_review / failed / 67% (FAIL) | answer_with_citation / passed / 100% (PASS) |
| `resignation_5y_exact` | answer_with_citation | legal_counsel_review / failed / 33% (FAIL) | answer_with_citation / passed / 100% (PASS) |
| `resignation_59m` | answer_with_citation | legal_counsel_review / failed / 50% (FAIL) | answer_with_citation / passed / 100% (PASS) |
| `death_1y` | answer_with_citation | legal_counsel_review / failed / 33% (FAIL) | answer_with_citation / passed / 100% (PASS) |
| `disablement_4y` | answer_with_citation | legal_counsel_review / failed / 33% (FAIL) | answer_with_citation / passed / 100% (PASS) |
| `maternity_80_days` | answer_with_citation | legal_counsel_review / failed / 50% (FAIL) | answer_with_citation / passed / 100% (PASS) |
| `maternity_creche_50` | answer_with_citation | legal_counsel_review / failed / 67% (FAIL) | answer_with_citation / passed / 100% (PASS) |
| `posh_committee_20` | answer_with_citation | legal_counsel_review / failed / 0% (FAIL) | answer_with_citation / passed / 100% (PASS) |
| `posh_timeline` | answer_with_citation | legal_counsel_review / failed / 33% (FAIL) | answer_with_citation / passed / 100% (PASS) |
| `historical_death_2019` | answer_with_citation | legal_counsel_review / failed / 50% (FAIL) | answer_with_citation / passed / 100% (PASS) |
| `historical_below_5y` | answer_with_citation | answer_with_citation / passed / 50% (FAIL) | answer_with_citation / passed / 100% (PASS) |
| `termination_dispute` | legal_counsel_review | legal_counsel_review / passed / 100% (PASS) | legal_counsel_review / passed / 100% (PASS) |
| `pf_question` | legal_counsel_review | legal_counsel_review / passed / 100% (PASS) | legal_counsel_review / passed / 100% (PASS) |

## Notes

- Groundedness and hallucination are claim-level checks. A run can fail verification on a missing qualifier while every individual claim remains grounded; that is why the two outcomes are reported separately.
- Context relevance is computed from the passages actually retrieved during the run, not from the final citation list. Unused but relevant passages count as relevant. The qualifier-aware variant deliberately retrieves more related material, so its precision can be lower even while its context recall is higher; both numbers are reported.
- Context recall uses the required-provision set. It isolates whether the decisive proviso, definition, or threshold reached the agent at all.
- The `outside` case expects zero relevant passages and no citation. An escalation is the correct outcome; answering it from the gratuity corpus would be a failure.
- The 1972 Act is intentionally present in the index as repealed. The evaluation checks that operative claims under the Code do not use it as authority, while the historical case does.
