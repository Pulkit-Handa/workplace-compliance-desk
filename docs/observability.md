# Observability

## What is instrumented

Every public boundary and every expensive internal step emits either a metric, a structured log event, or both:

- HTTP admission: runs accepted, cache hits, rate limits, quarantine, validation failures.
- Queue: accepted runs, backlog, pending deliveries, active runs in the worker.
- Graph: agent latency and outcomes, tool calls, steps per run, tool calls per run.
- Retrieval: latency by kind, passages returned, cache hits/misses.
- Verification: pass/fail counts and failure reasons.
- Provider: calls, tokens, cost, latency, failures, circuit-breaker state.
- Index: chunk count, build timestamp, build duration, embedded vs reused chunks.
- Quality: groundedness, context relevance, faithfulness, hallucination rate (live samples).
- Feedback: satisfactory/unsatisfactory counts by category.

The API exposes `/metrics`; the worker runs a separate Prometheus server on `WORKER_METRICS_PORT` (default `9101`).

## Declared SLO

The SLO is declared **before** measurement, in `docs/` and in `/v1/index`:

| Property | Target |
|---|---|
| End-to-end p95, submit → terminal status | **≤ 20,000 ms** |
| Sustained advisory throughput | **≥ 2 runs/s** |
| Error rate | 0 for the measured profile |
| Queue backlog after drain | 0 |

The measured Compose baseline is **p95 930 ms, 3.06 runs/s, 0 failures** on a
single-host topology with one worker and the deterministic stub provider. A stress test at
120 closed-loop users found the one-worker limit: **p95 22.0 s**, failing the SLO at the
worker/queue stage. Three workers kept the same profile at **p95 8.7 s and 13.27 runs/s**.

That is evidence for the SLO on this topology, not a capacity claim for a real model. The
SLO is not quietly redefined after the fact: the load report records the profile,
topology, failure concurrency, and caveats.

## Metric catalog

### Runs and outcomes

| Metric | Type | Labels |
|---|---|---|
| `compliance_runs_total` | counter | `status`, `route`, `domain` |
| `compliance_run_duration_seconds` | histogram | — |
| `compliance_run_cost_usd` | histogram | — |
| `compliance_runs_cached_total` | counter | — |
| `compliance_runs_degraded_total` | counter | — |
| `compliance_escalations_total` | counter | `reason`, `route` |
| `compliance_runs_queued_total` | counter | — |
| `compliance_active_runs` | gauge | — |

`compliance_run_duration_seconds` is the SLO histogram. Its buckets run from 250 ms to 120 s.

### Admission

| Metric | Type | Labels |
|---|---|---|
| `compliance_quarantine_total` | counter | `reason` |
| `compliance_rate_limited_total` | counter | `client` |
| `compliance_feedback_total` | counter | `satisfactory`, `category` |

### Agents and tools

| Metric | Type | Labels |
|---|---|---|
| `compliance_agent_duration_seconds` | histogram | `agent` |
| `compliance_agent_success_total` | counter | `agent`, `outcome` |
| `compliance_tool_calls_total` | counter | `tool`, `outcome` |
| `compliance_tool_duration_seconds` | histogram | `tool` |
| `compliance_steps_per_run` | histogram | — |
| `compliance_tool_calls_per_run` | histogram | — |
| `compliance_retries_total` | counter | `reason` |

### Verification

| Metric | Type | Labels |
|---|---|---|
| `compliance_verification_total` | counter | `outcome` (`passed`/`failed`) |
| `compliance_verification_failures_total` | counter | `reason` |

Failure reasons are the issue reasons from verification: `no_citation`, `citation_not_retrieved`, `not_in_force_on_date`, `weak_passage_support`, `number_not_supported`, `qualifier_missing`, `conflicting_status`, `unauthorised_determination`.

### Retrieval and index

| Metric | Type | Labels |
|---|---|---|
| `compliance_retrieval_latency_seconds` | histogram | `kind` |
| `compliance_passages_returned` | histogram | — |
| `compliance_cache_ops_total` | counter | `kind`, `result` |
| `compliance_index_chunks` | gauge | — |
| `compliance_index_build_timestamp_seconds` | gauge | — |
| `compliance_index_build_duration_seconds` | histogram | — |
| `compliance_index_embedded_chunks_total` | counter | — |
| `compliance_index_reused_chunks_total` | counter | — |

### Provider and circuit breaker

| Metric | Type | Labels |
|---|---|---|
| `compliance_llm_calls_total` | counter | `provider`, `task`, `outcome` |
| `compliance_llm_tokens_total` | counter | `kind` |
| `compliance_llm_cost_usd_total` | counter | — |
| `compliance_llm_latency_seconds` | histogram | `provider`, `task` |
| `compliance_provider_failures_total` | counter | `reason` |
| `compliance_circuit_breaker_state` | gauge | `state` |
| `compliance_circuit_breaker_transitions_total` | counter | `to_state` |

### Queue

| Metric | Type | Meaning |
|---|---|---|
| `compliance_queue_depth` | gauge | Unprocessed entries: consumer-group **lag + pending** |
| `compliance_queue_pending_entries` | gauge | Delivered but not yet acknowledged |
| `compliance_active_runs` | gauge | Runs currently executing in this worker |

The queue-depth definition matters. `XLEN` includes acknowledged entries that remain in the stream until trimming, so an `XLEN`-based gauge reports a permanent backlog after the first burst. `depth()` was corrected to use lag + pending, with `XLEN - pending` as a fallback when lag is unavailable. The load test confirms `compliance_queue_depth 0` after the backlog drains while `XLEN` is still non-zero.

### Quality gauges

| Metric | Type | Labels |
|---|---|---|
| `compliance_quality_groundedness` | gauge | `source` |
| `compliance_quality_context_relevance` | gauge | `source` |
| `compliance_quality_faithfulness` | gauge | `source` |
| `compliance_quality_hallucination_rate` | gauge | `source` |

These are sampled live-run gauges, not a replacement for the golden-set evaluation.
The Python client also exports the standard `process_resident_memory_bytes` and
`process_cpu_seconds_total`; dashboard panels 13 and 14 use them for per-service
resource visibility without a privileged cAdvisor sidecar. Panels 15 and 16 use the
Prometheus `up` target state and `process_start_time_seconds` for availability and
uptime.

## Logs

`src/compliance/logging_setup.py` emits one JSON object per line to stdout:

```json
{
  "ts": "2026-10-26T13:47:58.236Z",
  "level": "INFO",
  "service": "compliance-api",
  "logger": "compliance.api",
  "event": "run_accepted",
  "correlation_id": "corr_...",
  "run_id": "run_...",
  "agent": "",
  "step": "",
  "client_id": "compose-smoke",
  "variant": "v2",
  "queue": "RedisStreamQueue"
}
```

Context variables (`correlation_id`, `run_id`, `agent`, `step`) are propagated with `log_context`, so a log line from a nested tool call still identifies the run. Agent/tool events include latency, outcome, and budget counters. Exceptions include a formatted traceback.

Useful events:

| Event | Where |
|---|---|
| `api_started`, `api_stopped` | API lifespan |
| `run_accepted`, `run_cached` | API |
| `run_executed` | Runner |
| `advisory_completed` | Graph |
| `tool_call` / `tool_result` | Tools |
| `verification` | Verify node |
| `llm_call`, `llm_fallback` | Provider |
| `resource_close_failed` | Shutdown |
| `index_loaded` | Index |
| `worker_started` / `worker_stopped` | Worker |

Common filters:

```bash
docker compose logs -f api worker | jq -c 'select(.event=="run_executed")'
docker compose logs worker | jq -c 'select(.level=="ERROR")'
docker compose logs api | jq -c 'select(.run_id=="run_...")'
```

## Traces

OpenTelemetry is wired in Compose. The API and worker export OTLP/HTTP to the project
Jaeger instance at `http://jaeger:4318/v1/traces`; Jaeger's UI is published on
**http://localhost:16687** (the internal OTLP port is not published, which avoids host-port
collisions). Sampling is parent-based ratio sampling (`OTEL_TRACES_SAMPLER_ARG=1.0` in the
demo). Without `OTEL_EXPORTER_OTLP_ENDPOINT`, the SDK uses a no-op tracer and the service
runs normally.

`instrument_fastapi` excludes `/health/live`, `/health/ready`, and `/metrics`. HTTPX and
Redis clients are instrumented when the libraries are present. Setup failures are logged
and never stop the service. Verify the export with:

```bash
curl -fsS http://localhost:16687/api/services
# {"data": ["compliance-api"], ...}
```

The **run trace** is separate from OTel and is always stored:
`GET /v1/advisories/{run_id}/trace` returns the ordered agent/tool/verification events with
sequence, timestamp, elapsed milliseconds, and structured fields. Tool events include the
retrieved passage ids/citations with bounded previews; model events include prompt
version, tokens, and per-call cost. This is the trace a reviewer or incident responder
uses; OTel is for cross-service timing.

The concrete verification (service list, trace ID, scrape targets, alert count, and
dashboard panels) is recorded in
[`reports/observability_evidence.md`](../reports/observability_evidence.md).

## Dashboard

Grafana provisions the datasource and the **Workplace Compliance Advisory Desk** dashboard. Panels:

1. Advisory throughput by route
2. End-to-end latency (p50 / p95 / p99)
3. Route distribution
4. Verification outcomes
5. Escalations by reason
6. Queue depth, pending and active runs
7. Whole-run cache hits
8. Provider failures and degraded runs
9. Sampled live quality
10. Ingestion quarantine and rate limiting
11. Index chunks
12. Feedback satisfaction
13. Service resident memory (`process_resident_memory_bytes` by job)
14. Service CPU (`rate(process_cpu_seconds_total)` by job)
15. Service availability (`up` by job)
16. Service uptime (`time() - process_start_time_seconds` by job)
17. Agent latency p95 by agent
18. Agent success rate by agent
19. Steps and tool calls per run
20. Research retries and retry rate

Panels 13 and 14 are the per-service container process resources. They are deliberately
labelled as process rather than host metrics: the app containers expose them directly, so
the dashboard does not need a privileged cAdvisor container. Host-level disk/network is
left to the platform's node exporter.

Open http://localhost:3000 (`admin` / `admin` by default in Compose; change it for any
shared environment).

## Alerts

`monitoring/prometheus/alerts.yml`:

| Alert | Expression | For | Severity |
|---|---|---|---|
| `AdvisoryLatencyP95Breached` | p95 run duration > 20 s | 5m | warning |
| `QueueBacklogGrowing` | `compliance_queue_depth > 50` | 5m | warning |
| `ProviderFailuresElevated` | failure rate > 0.2/s | 5m | warning |
| `QuarantineSpike` | quarantine rate > 1/s | 5m | info |
| `VerificationFailureRate` | failed / total > 25% | 15m | warning |
| `GroundednessBelowFloor` | live groundedness < 0.85 | 15m | warning |

Prometheus scrapes `api:8000`, `worker:9101`, and `stub-llm:8080` every 10 seconds. The
alert rules and scrape config are mounted read-only with SELinux `:ro,z` labels in Compose.
The worker publishes no host port, so it can be scaled (`docker compose up -d --scale
worker=3`); inspect a replica with `docker compose exec -T worker ...` or use Prometheus.

## Health and readiness

| Endpoint | Meaning |
|---|---|
| `GET /health/live` | Process is alive; no dependency checks |
| `GET /health/ready` | Index, store, queue, cache, limiter checks; returns `503` if a required check fails |
| `GET /metrics` | Prometheus exposition |

Example readiness:

```json
{
  "status": "ok",
  "checks": {"index": true, "store": true, "queue": true, "cache": true, "limiter": true},
  "index_version": "idx-1793020189-b6107331"
}
```

The response is served by the API; the worker has its own liveness through its metrics server and queue loop. The API healthcheck in the image is `curl -fsS http://127.0.0.1:8000/health/live`.

## Operational queries

```promql
# p95 end-to-end latency over 5 minutes
histogram_quantile(0.95, sum(rate(compliance_run_duration_seconds_bucket[5m])) by (le))

# runs per second by route
sum(rate(compliance_runs_total[5m])) by (route)

# verification failure mix
sum(rate(compliance_verification_failures_total[15m])) by (reason)

# backlog
compliance_queue_depth
compliance_queue_pending_entries
compliance_active_runs

# provider health
sum(rate(compliance_provider_failures_total[5m])) by (reason)
compliance_circuit_breaker_state

# cache effectiveness
sum(rate(compliance_cache_ops_total{result="hit"}[5m]))
/
clamp_min(sum(rate(compliance_cache_ops_total[5m])), 0.001)
```

## What is not observable (yet)

- Per-tenant cost attribution beyond the `client_id` label on rate limits.
- A production-grade trace backend is not included; OTel export is configured but no collector is bundled.
- Quality gauges are sampled/derived; they do not replace human review of legal outputs.
- The stub provider's metrics are useful for failure injection, not for model quality.
