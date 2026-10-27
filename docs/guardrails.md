# Guardrails and ingestion

## One boundary

All public requests pass through `validate_payload` in `src/compliance/ingestion.py`. There is no second, “trusted” path into the agents. A request is either:

- accepted as a validated `ComplianceRequest`, or
- rejected with stable field errors, quarantined, and **no run created**.

The API never calls the graph directly; it calls the ingestion boundary, then the queue.

## Admission sequence

```
raw body (≤ 64 KiB)
  │
  ├─ JSON parse ─────────────── invalid ──▶ 422 + quarantine(reason=invalid_json)
  │
  ├─ free-text scan ─────────── quarantine/high ──▶ 422 + quarantine(reason=guardrail)
  │                         └── sanitise/medium ──▶ replace instruction spans
  │
  ├─ strict Pydantic schema ─── invalid/unknown ──▶ 422 + quarantine(reason=schema)
  │
  ├─ consent + cross-field ──── invalid ──▶ 422 + quarantine(reason=schema)
  │
  └─ accepted ──────────────────────────────▶ rate limit → cache → queue
```

The body-size check happens before JSON parsing when the body is bytes. The body limit is **64 KiB**, independent of field length limits.

## Schema

`ComplianceRequest` (`src/compliance/contracts.py`) uses `extra="forbid"` and strict enums. Key rules:

- `question`: 10–2,000 characters.
- `consent`: must be present and `true`; the caller attests the request contains synthetic data only.
- `as_of_date`: defaults to today; cannot precede an employment date that is in the future relative to it.
- `employment.start_date < end_date`; both cannot be after `as_of_date`.
- `employment.completed_service_months` must agree with supplied dates within one month.
- `event=expiry` requires `worker_type` `fixed_term` or `contract` (or `unknown`).
- `fixed_term` with an `end_date` also requires a `start_date`.
- `notes`: optional, ≤ 2,000 characters.
- Unknown fields are rejected rather than ignored.

The schema is intentionally boring. Legal correctness is enforced later by retrieval and verification, not by trying to encode every legal nuance into request validation.

## Synthetic-data attestation

`consent=true` is a **required attestation**, not a checkbox with a default. A missing or false value produces:

```json
{
  "error": "validation_failed",
  "field_errors": [
    {"field": "consent", "code": "missing", "message": "Field required"}
  ],
  "quarantine_id": "quar_...",
  "correlation_id": "corr_..."
}
```

No run ID is issued. The quarantine record is written to `QUARANTINE_DIR/<YYYY-MM-DD>/<quarantine_id>.json` and includes:

- reason (`schema`, `guardrail`, `invalid_json`, `body_too_large`)
- field errors
- received timestamp
- body SHA-256
- truncated body (16,000 characters) for debugging

Quarantine files contain caller-supplied synthetic text; they are not used for training and should still be access-controlled.

## Prompt injection and toxicity

`src/compliance/guardrails/__init__.py` scans these free-text fields:

- `question`
- `notes`
- `establishment.name`
- `establishment.state`
- `requester.organisation`

The patterns target **instructions to the model**, not legal subject matter. For example, “harassment” and “termination” are legitimate; “ignore previous instructions” and “reveal your system prompt” are not.

Injection patterns include: instruction override, prompt exfiltration, role override, jailbreak phrases, tool directives, destructive directives, exfiltration, policy override, and role tokens such as `<|system|>`.

Toxicity patterns cover threats, slurs, and direct abuse. They are deliberately conservative; a compliance question may quote a hostile message in a POSH context, and the pattern set does not try to police quoted evidence.

Modes are configurable per class:

```
INJECTION_MODE=quarantine   # or sanitise
TOXICITY_MODE=quarantine    # or sanitise
```

- **high** severity + `quarantine` → block the request, return `422`, write quarantine.
- **high** severity + `sanitise` → neutralise instruction spans and continue.
- **medium** severity → sanitise.
- No findings → pass through unchanged.

Sanitisation replaces hits with `[removed-instruction]` and neutralises angle brackets. The sanitised text is what reaches the schema and the graph.

## Output guardrail

The advisory is not allowed to issue a legal determination or predict an outcome. `filter_output` scans the composed text for phrases such as:

- “we determine/decide/conclude/find that”
- “our final/binding decision”
- “you will win”
- “the court will hold/find/decide”
- “we advise you to sue/litigate/file a case”
- “legally liable/guilty”
- “we are your lawyer/attorney/counsel”
- “guaranteed outcome/result/compensation”

Matches are replaced with `[removed: not authorised to issue a legal determination]` and recorded in `guardrail_report.output_filtered_phrases`. Filtering does not turn a bad answer into a good one: verification and routing have already run. It is a last line of defence for wording.

Every result also carries the fixed disclaimer:

> This is a cited compliance advisory, not legal advice or a legal opinion. A qualified professional must decide the matter.

## Rate limiting

The public API uses a per-client token bucket (`src/compliance/service/ratelimit.py`):

- Identity: `X-Client-Id` if present, otherwise the client host/requester identity.
- Defaults: `RATE_LIMIT_PER_MINUTE=60`, `RATE_LIMIT_BURST=10`; the Compose stack raises these to `600` / `60` for the load profile.
- Redis-backed for multi-process deployments, with a local in-process floor when Redis is optional.
- Exceeded → `429` with `Retry-After` and a stable error body. The request is not enqueued.
- The limiter is exercised by the Locust profile, which gives each simulated user a distinct client id so the whole test does not collapse into one bucket.

The token bucket is admission control, not a fairness scheduler. A single client cannot exhaust the worker, but it can still create a backlog within its allowance; `compliance_queue_depth` and worker concurrency handle that.

## Additional protections

| Protection | Where | Effect |
|---|---|---|
| Request body cap | `ingestion.py` | 64 KiB; larger bodies quarantined |
| Strict Pydantic contract | `contracts.py` | Unknown fields and invalid enums rejected |
| No network tools | `tools/__init__.py` | Agents cannot browse or call arbitrary APIs |
| Tool budget | `agents/state.py` | `MAX_TOOL_CALLS=10` per run |
| Step/wall-clock/LLM budgets | `agents/state.py` | Bounded execution; budget breach escalates |
| Prompt artifacts | `prompts/` | Versioned, hashed, exposed via `/v1/prompts` |
| Output filter | `guardrails/__init__.py` | Removes unauthorised determinations |
| Fixed disclaimer | `guardrails/__init__.py` | Attached to every advisory |

## Quarantine operational notes

- Directory: `QUARANTINE_DIR` (Compose: `/data/quarantine`, host volume `compliance_quarantine` or configured bind mount).
- One JSON file per rejected request, grouped by UTC date.
- The API response includes `quarantine_id` and `correlation_id` for support.
- Quarantine writes are best-effort but logged; a write failure does not cause the rejected request to be accepted.
- There is no automatic replay. A quarantined request is evidence of a rejection, not a queued job. Re-submission requires a corrected request.

## What is deliberately not guarded

- **Subject-matter words** are not blocked. “Harassment”, “termination”, “strike”, and “discrimination” are legitimate compliance vocabulary.
- **Factual disagreement** is not a guardrail failure. It is handled by verification and escalation.
- **Model uncertainty** is not trusted as a safety signal. The deterministic verifier decides whether a claim survives.
- **PII detection** is not attempted beyond the synthetic-data attestation. The contract forbids real personal data; the system does not claim to be a PII scrubber.
