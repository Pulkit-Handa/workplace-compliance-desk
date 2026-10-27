# API

Base URL in Compose: `http://localhost:8000`. OpenAPI is available at `/docs` and `/openapi.json`.

All requests and responses are JSON unless noted. Every response carries a correlation ID (body or logs); the API also returns it in error bodies.

## Headers

| Header | Optional | Purpose |
|---|---|---|
| `Content-Type: application/json` | no | Required for POST bodies |
| `X-Client-Id` | yes | Rate-limit identity; defaults to the requester client id |
| `X-Prompt-Variant` | yes | `v1` or `v2`; defaults to `PROMPT_VARIANT` (`v2`) |

An invalid variant returns `400` with `{"error": "invalid_variant", ...}`.

## POST `/v1/advisories`

Validate, guardrail, rate-limit, cache-check, and enqueue a run.

### Request

```json
{
  "question": "A fixed term employee's contract expires after 3 years. Are we liable for gratuity under the Payment of Gratuity Act 1972 or the Code on Social Security, 2020?",
  "establishment": {
    "name": "Example Synthetic Services",
    "establishment_type": "it_services",
    "employee_count": 120,
    "state": "Karnataka",
    "has_internal_committee": true
  },
  "employment": {
    "worker_type": "fixed_term",
    "start_date": "2022-10-01",
    "end_date": "2025-10-01",
    "event": "expiry",
    "completed_service_months": 36,
    "monthly_wage_inr": 50000
  },
  "requester": {
    "client_id": "demo-hr",
    "role": "hr",
    "organisation": "Example Synthetic Services"
  },
  "as_of_date": "2025-11-25",
  "consent": true,
  "notes": "Synthetic example only."
}
```

Required fields:

- `question`, `establishment`, `employment`, `requester`, `consent`.
- `consent` must be exactly `true`.
- `requester.client_id` is 1–64 characters of `[A-Za-z0-9_.:-]`.
- Unknown fields are rejected.

`as_of_date` defaults to today. It drives the in-force check and the historical-vs-current gratuity branch.

### Accepted — `202`

```json
{
  "run_id": "run_4c38d90396a8421297bb",
  "correlation_id": "corr_309f3559fc4f498bb9d8",
  "status": "queued",
  "poll_url": "/v1/advisories/run_4c38d90396a8421297bb",
  "estimated_seconds": 15,
  "cached": false
}
```

A cache hit returns **`200`** with `status` already terminal and `cached=true`.

### Rejected — `422`

```json
{
  "error": "validation_failed",
  "correlation_id": "corr_...",
  "quarantine_id": "quar_f6c1a1698caf4cd4b616",
  "field_errors": [
    {"field": "consent", "code": "missing", "message": "Field required"}
  ],
  "agent_started": false,
  "message": "Request rejected at the ingestion boundary. No agent run was started."
}
```

No run is created. The request is written to `QUARANTINE_DIR/<date>/<quarantine_id>.json`.

### Rate limited — `429`

```json
{
  "error": "rate_limited",
  "correlation_id": "corr_...",
  "retry_after_ms": 8200
}
```

Header: `Retry-After: <seconds>`.

## GET `/v1/advisories/{run_id}`

Returns a `RunEnvelope`:

```json
{
  "run_id": "run_...",
  "correlation_id": "corr_...",
  "status": "completed",
  "progress": "route: answer_with_citation",
  "result": { "...": "AdvisoryResult" },
  "error": null,
  "created_at": "2026-10-26T13:47:58.236Z",
  "updated_at": "2026-10-26T13:47:59.300Z"
}
```

Status transitions:

```
queued → running → completed
                ↘ escalated
                ↘ failed
```

`completed` and `escalated` both carry a result. `failed` carries `error` and may have a partial result.

Unknown or expired runs return `404`.

### AdvisoryResult

| Field | Meaning |
|---|---|
| `run_id`, `correlation_id`, `status` | Identity and terminal status |
| `route` | `answer_with_citation`, `request_more_establishment_detail`, or `legal_counsel_review` |
| `domain` | `gratuity`, `maternity_benefit`, `workplace_harassment`, or `other` |
| `advisory` | The composed advisory text, with citations |
| `summary` | Short summary |
| `claims[]` | Claim text, kind, citations, supported flag, support score, reason |
| `citations[]` | Provision metadata, quote, source URL, status, effective dates, support score |
| `verification` | Outcome, claims checked/supported, issues, loops, checks, notes |
| `route_decision` | Route, basis, confidence, escalation path, required fields, next actions |
| `guardrails` | Input action, findings, output-filtered phrases |
| `prompt_versions` | Prompt artifact versions/hashes used |
| `usage` | Token, cost, and latency accounting per step |
| `latency_ms`, `steps`, `tool_calls` | Run accounting |
| `degraded`, `cached` | Whether a fallback path or cache was used |
| `index_version` | Index used for the run |
| `created_at`, `completed_at` | Timestamps |
| `disclaimer` | Fixed advisory disclaimer |

`usage` contains the run totals and `usage.per_step[]` contains one entry per model
call (`intake`, `draft`, `verify`, `route`) with prompt tokens, completion tokens,
`cost_usd`, latency, provider, model, and prompt version. A cache hit returns the
original result and its original logical usage; the incremental provider cost is zero
because the API does not enqueue or call the model. See
[`reports/cost_report.md`](../reports/cost_report.md).

Example excerpt:

```json
{
  "status": "completed",
  "route": "answer_with_citation",
  "domain": "gratuity",
  "verification": {
    "outcome": "passed",
    "claims_checked": 9,
    "claims_supported": 9,
    "issues": [],
    "checks": [
      "citation_retrieved",
      "in_force_on_as_of",
      "passage_support",
      "numeric_consistency",
      "qualifier_completeness",
      "no_unauthorised_determination"
    ]
  },
  "route_decision": {
    "route": "answer_with_citation",
    "basis": "Every operative claim cites a provision that is in force on the requested date.",
    "confidence": 0.9
  }
}
```

## GET `/v1/advisories/{run_id}/trace`

Returns the stored run trace:

```json
{
  "run_id": "run_...",
  "correlation_id": "corr_...",
  "request": { "...": "normalised request" },
  "events": [
    {
      "seq": 1,
      "ts": "2026-10-26T13:47:58.300Z",
      "elapsed_ms": 0.2,
      "kind": "step",
      "agent": "intake",
      "name": "classify",
      "data": {"domain": "gratuity", "risks": ["repeal"]}
    }
  ],
  "result": { "...": "AdvisoryResult" }
}
```

Event kinds include `step_start`, `step_end`, `tool_call`, `llm_call`, `verification`,
`agent_note`, `degraded`, `cache_hit`, and error events. A `tool_call` carries the query,
reason, item count, and a `passages[]` list with each retrieved chunk's id, citation,
status, score, and a bounded text preview. An `llm_call` carries the prompt version,
prompt/completion tokens, per-call `cost_usd`, provider latency, and fallback flag. The
trace is intended for reviewers and incident response; it is not an OTel span dump.

## POST `/v1/advisories/{run_id}/feedback`

```json
{
  "satisfactory": false,
  "reason": "Citation did not address the fixed-term proviso.",
  "category": "citation"
}
```

Categories: `accuracy`, `citation`, `missing_context`, `escalation`, `latency`, `other`.

Response:

```json
{"feedback_id": "fb_...", "run_id": "run_...", "recorded": true}
```

Feedback is stored with the run for `RUN_TTL_S` and aggregated in `/v1/feedback/summary`. Unknown runs return `404`. Invalid feedback returns `422`.

## GET `/v1/feedback/summary`

```json
{
  "total": 4,
  "satisfactory": 3,
  "unsatisfactory": 1,
  "satisfaction_rate": 0.75,
  "by_category": {"citation": 1},
  "recent": []
}
```

## GET `/v1/index`

Operational metadata:

- `index_version`, `built_at`, `age_hours`
- `chunk_count`, `provision_count`, `corpus_hash`
- `embedding`: provider, fingerprint, dimension
- `chunking`: strategy, max chars, overlap, config
- `documents[]`: status, effective window, source URL, source hash, provision count
- `prompts`: active variant and artifact manifest
- `slo`: p95 target, target rps, `declared_before_measurement`
- `routing`: route meanings

## GET `/v1/prompts`

Returns the prompt registry manifest: active variant, available variants, artifact paths, versions, and hashes. The cache key includes these hashes, the index version, and a policy fingerprint.

## GET `/v1/demo/cases`

Returns synthetic demo payloads used by the UI. No real personal data.

## GET `/v1/scope`

Returns in-scope topics, out-of-scope topics, and the disclaimer.

## GET `/health/live`

```json
{"status": "ok", "service": "compliance-api"}
```

No dependency checks.

## GET `/health/ready`

```json
{
  "status": "ok",
  "checks": {"index": true, "store": true, "queue": true, "cache": true, "limiter": true},
  "index_version": "idx-1793020189-b6107331"
}
```

Returns `503` with `status: "degraded"` if any required check fails. Use this for orchestration readiness.

## GET `/metrics`

Prometheus text exposition. Excluded from OpenAPI and from FastAPI tracing.

## Error format

Most errors use:

```json
{"error": "<code>", "detail": "<human readable>", "correlation_id": "corr_..."}
```

Validation uses the quarantine shape above. `404` uses:

```json
{"error": "not_found", "run_id": "run_...", "detail": "unknown or expired run"}
```

## Polling recipe

```bash
RUN_ID=$(curl -sS -X POST http://localhost:8000/v1/advisories \
  -H 'content-type: application/json' \
  -d @request.json | jq -r .run_id)

while true; do
  BODY=$(curl -sS "http://localhost:8000/v1/advisories/$RUN_ID")
  STATUS=$(jq -r .status <<<"$BODY")
  case "$STATUS" in
    completed|escalated|failed) jq . <<<"$BODY"; break ;;
  esac
  sleep 0.5
done
```

The UI does the same with a bounded poll interval and a timeout.

## Contract notes

- `AdvisoryResult` is the same object returned inside `RunEnvelope` and `RunTrace`, so downstream tools can reuse one parser.
- Dates are ISO `YYYY-MM-DD`; timestamps are UTC ISO-8601 with `Z`.
- Enum values are lower-case strings in JSON.
- The cache key is not exposed; a cache hit is indicated by `cached=true` and a `cache_hit` trace event.
- Runs expire after `RUN_TTL_S` (default 7 days); an expired run returns `404`.
