# Verification

## Why a verifier

A fluent answer can be wrong in three distinct ways:

1. it cites a provision that was never retrieved (fabricated authority);
2. it cites a real provision that was not in force on the relevant date (repealed or not yet commenced);
3. it cites the correct main section but omits the proviso that changes the result (incomplete authority).

Verification is a deterministic, claim-by-claim pass that catches all three. The drafting model never decides whether its own output is acceptable. The route is derived from the verification result, not from model confidence.

## What a claim is

`policy.compose` builds claims from deterministic rules plus retrieved evidence. A claim has:

- `id`, `text`, `kind` (`operative` or context/escalation), and `citations`;
- `supported` and `support_score`, filled by the verifier;
- an optional `reason` for the first failure.

Every operative sentence must have at least one citation. An escalation claim is allowed to have none, but it is explicitly marked `kind="escalation"` and never presented as a legal conclusion.

## The checks

`verify(ctx)` runs these checks:

| Check | What it enforces | Failure reason |
|---|---|---|
| `citation_retrieved` | Every citation ID exists in this run's evidence map | `no_citation`, `citation_not_retrieved` |
| `in_force_on_as_of` | For operative claims, `effective_from ≤ as_of ≤ effective_to` | `not_in_force_on_date` |
| `passage_support` | Content-word overlap between claim and cited passage meets the threshold | `weak_passage_support` |
| `numeric_consistency` | Substantive numbers in an operative claim occur in the cited text or are caller facts | `number_not_supported` |
| `qualifier_completeness` | Every fact-pattern qualifier is in the draft's operative citations | `qualifier_missing` |
| `no_unauthorised_determination` | No claim decides a contested fact or legal outcome | `unauthorised_determination` |

A seventh consistency check rejects a claim that mixes a repealed and an in-force provision as operative authority (`conflicting_status`).

### 1. Citation was retrieved in this run

The evidence map is populated by the research tools. A citation ID that is merely plausible, or that exists in the corpus but was not retrieved, fails. This makes the retrieval trace and the answer inseparable.

### 2. In force on the requested date

`_in_force_on` parses `effective_from`/`effective_to` and checks the request's `as_of` date. Operative claims only. Context claims may describe historical law, but they cannot be used to ground a current entitlement.

This is the control that prevents the repealed Payment of Gratuity Act, 1972 from being used as current authority after **20 November 2025**.

### 3. Passage support

`_support` is content-word overlap:

```
supported tokens / claim content tokens
```

Tokens are normalised and stop-word filtered by `match_tokens`. Citation markers are removed before numeric comparison so “s. 60(3)” does not contribute 60 and 3 as substantive numbers.

Thresholds:

- **operative claims: 0.45**
- context/non-operative claims: 0.18

A model draft is only accepted at draft time if every claim achieves at least **0.5** (`nodes._claims_from_llm`). Verification then applies its own thresholds. The draft-time threshold is slightly stricter on purpose: the deterministic policy draft is always the safe floor.

### 4. Numeric consistency

An operative claim may not introduce a number that is absent from its cited text. Exceptions:

- numbers supplied by the caller (`employee_count`, service months, wage, children, dates) are facts, not legal propositions;
- section, proviso, and appeal numbers are removed by `_without_section_refs`;
- only positive numbers with at most six digits are considered, which avoids noise from long identifiers.

This catches a model inventing a threshold, a rate, or a deadline.

### 5. Qualifier completeness

`policy.qualifier_provisions(ctx)` returns a map of provision ID → reason. For example:

| Fact pattern | Required qualifier |
|---|---|
| death or disablement | second proviso to s. 53(1) — five-year waiver |
| fixed-term expiry | second proviso to s. 53(1) |
| fixed-term worker | third proviso to s. 53(2) — pro-rata rate |
| working journalist | first proviso to s. 53(1) — three years |
| any gratuity answer | First Schedule, Chapter V — coverage threshold |
| question mentions repeal | s. 164(1) — repeal |
| POSH answer | s. 4 — Internal Complaints Committee |
| maternity answer | s. 60 and First Schedule, Chapter VI |

A missing qualifier is a failure **even when the main section is perfectly quoted**. This is the central difference between a plausible answer and a complete one. It is also why the v1 baseline scores 40% qualifier coverage on the 24-case set while v2 scores 100%.

### 6. No unauthorised determination

A claim may state what a provision says. It may not decide that a person is guilty, liable, harassed, or at fault. `_DETERMINATION_RE` catches constructions such as “the employee is liable” or “the complainant was harassed”. The output filter provides a second, wording-level guard.

### Conflicting status

If one operative claim cites both a repealed and an in-force provision as authority for the same proposition, verification adds `conflicting_status` and marks the claim unsupported. This prevents a draft from hedging across two regimes and presenting both as current.

## Outcome

- All claims supported + all qualifiers present + no issues → `PASSED`.
- Any issue → `FAILED`, with a de-duplicated issue list.

Each claim stores its support score; each issue carries a `suggested_query` where one can be constructed. Failure counters are emitted as `compliance_verification_failures_total{reason=...}`.

`VerificationResult` includes:

- `outcome`
- `claims_checked`, `claims_supported`
- `issues[]` (`claim_id`, `reason`, `detail`, `suggested_query`)
- `loops`
- `checks[]` (the list above)
- `notes`

## Retry loop

The graph routes a failed verification back to research, up to `MAX_VERIFY_LOOPS=2` (three attempts total including the first).

On retry:

1. Issue suggestions become `verification_gap` queries with `top_k=4`.
2. Required and qualifier provisions are re-fetched.
3. The draft is replaced, never appended: `ctx.claims = []` and `ctx.citations = {}` before re-composition.
4. Verification runs again.

If the loop is exhausted and verification still fails, the route is `legal_counsel_review`.
The unverified draft is not published: `route_node` clears `ctx.claims` and `ctx.citations`
before the response, so the caller receives the escalation summary, route basis, and issue
list, not the failed claim or its citation. The regression test is
`tests/test_golden_matrix.py::test_failed_draft_is_withheld_and_escalated`.

## Routing after verification

`policy.decide_route` is deterministic and ordered. First matching rule wins:

| Condition | Route |
|---|---|
| Domain is `OTHER` (no indexed provision) | `legal_counsel_review` |
| Risk includes `harassment_complaint` | `legal_counsel_review` |
| Risk includes `termination_dispute` | `legal_counsel_review` |
| Verification failed | `legal_counsel_review` |
| Coverage is unknown (for example missing headcount) | `request_more_establishment_detail` |
| Otherwise | `answer_with_citation` |

A model-proposed route is recorded in the trace but does not override this policy.

### Escalation look

For a harassment complaint, for example, the escalation names the Internal Complaints Committee under s. 4, confidentiality under s. 16, the inquiry procedure under s. 11, and the need for counsel before adverse action. The desk does not decide the complaint.

For a termination dispute, it asks for the service record, notices, and cited provisions, and routes to counsel.

## What the verifier does not do

- It does not judge whether the statute is morally or commercially fair.
- It does not decide contested facts.
- It does not replace counsel or the Internal Complaints Committee.
- It does not prove that every relevant provision was retrieved; it proves that the claims made are supported and that the fact pattern's known qualifiers were addressed.
- It is not a semantic entailment model. Support is lexical overlap plus deterministic rules. The design favours auditability over recall; the optional sentence-transformer embedder improves retrieval, not entailment.

## Inspecting a result

```bash
curl -sS http://localhost:8000/v1/advisories/<run_id> | jq '{
  status,
  route: .result.route_decision.route,
  verification: .result.verification,
  claims: [.result.claims[] | {id, kind, supported, support_score, reason, citations}]
}'
```

The trace records every verification attempt:

```bash
curl -sS http://localhost:8000/v1/advisories/<run_id>/trace | jq '.events[] | select(.agent=="verify")'
```

## Related tests

- `tests/test_verification.py` — support thresholds, numeric checks, repeal/status, qualifier failures.
- `tests/test_golden_matrix.py` — runs the golden set through the real graph and asserts route/verification outcomes.
- `tests/test_corpus_retrieval.py` — as-of retrieval, provision lookup, hybrid ranking.
- `tests/test_api.py` — result contract, citations, trace, feedback, quarantine.
