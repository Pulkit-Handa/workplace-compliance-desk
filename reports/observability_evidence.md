# Observability evidence

Measured on the running Compose stack on **2026-10-26**, after the final rebuild. This
file records the checks that a reviewer can repeat; it is not a screenshot gallery.

## Services

```text
api         compliance-desk-api-1         healthy
worker      compliance-desk-worker-1      healthy
redis       compliance-desk-redis-1       healthy
stub-llm    compliance-desk-stub-llm-1    healthy
jaeger      compliance-desk-jaeger-1      up
prometheus  compliance-desk-prometheus-1  up
grafana     compliance-desk-grafana-1     up
```

## Jaeger

- UI: http://localhost:16687
- OTLP from API/worker: `http://jaeger:4318/v1/traces` on the Compose network
- `GET /api/services` returned `compliance-api` (plus the all-in-one's own service)
- An advisory admission trace is stored:

```text
traceID             6d31c2bab31c56f936d48c4b13f607a5
operation           POST /v1/advisories
span kind           server
http.status_code    202
server duration     15.210 ms
spans in trace      5
```

Query it in the UI or with:

```bash
curl -fsS 'http://localhost:16687/api/traces/6d31c2bab31c56f936d48c4b13f607a5'
```

The run-level trace remains available even if the OTLP backend is down:
`GET /v1/advisories/{run_id}/trace`. The most recent fixed-term audit run returned 26
ordered events, including 32 retrieved-passage previews across eight tool calls and four
LLM calls with per-call cost.

## Prometheus

- Targets: `compliance-api` up, `compliance-worker` up, `stub-llm` up
- Alert group `compliance-desk-slo` loaded with 6 rules
- Worker metrics after the final audit runs:

```text
compliance_run_duration_seconds_count       3
compliance_verification_total{outcome="passed"} 3
compliance_llm_tokens_total{kind="prompt"}  17511
compliance_llm_tokens_total{kind="completion"} 12
compliance_llm_cost_usd_total               0.0026338500000000005
compliance_retries_total{reason="verification_failure"} 0
compliance_queue_depth                      0
compliance_queue_pending_entries            0
compliance_active_runs                      0
```

The API-side cache counter was `compliance_runs_cached_total 1` after the repeat request; quarantine counters were `schema=2` and `guardrail=1`. A
cache hit is served by the API and does not add worker provider cost.

## Grafana

- URL: http://localhost:3000 (admin / admin in the demo Compose profile)
- Dashboard: **Workplace Compliance Advisory Desk**, 20 panels
- Resource visibility:

| Panel | Query |
|---|---|
| 13. Service resident memory | `sum by (job) (process_resident_memory_bytes{job=~"compliance-.*"})` |
| 14. Service CPU (process) | `sum by (job) (rate(process_cpu_seconds_total{job=~"compliance-.*"}[$interval]))` |
| 15. Service availability | `up{job=~"compliance-api|compliance-worker|stub-llm"}` |
| 16. Service uptime | `time() - process_start_time_seconds{job=~"compliance-.*"}` |
| 17. Agent latency p95 | `histogram_quantile(0.95, sum by (le, agent) (rate(compliance_agent_duration_seconds_bucket[$interval])))` |
| 18. Agent success rate | `sum by (agent) (...) / clamp_min(sum by (agent) (...), 1e-9)` |
| 19. Steps and tool calls per run | histogram `_sum / _count` ratios |
| 20. Research retries and retry rate | `rate(compliance_retries_total[$interval])` and per-run ratio |

These are the application processes' own `process_*` series, not a privileged cAdvisor
sidecar. The panels show `compliance-api` and `compliance-worker` when both are being
scraped.

## Scaling caveat

The worker has no published host port, so `docker compose up -d --scale worker=3` works.
Prometheus scrapes the Compose DNS name `worker:9101`; with Docker's DNS behavior that
resolves to one worker replica per scrape, not all replicas. The aggregate service-level
evidence is therefore the Locust run and the Redis consumer-group state, while the
dashboard is best read as a per-process sample. The scaling measurements are in
[`reports/load_report.md`](load_report.md).
