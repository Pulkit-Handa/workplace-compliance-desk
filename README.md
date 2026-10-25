# Workplace Compliance Advisory Desk

A production-style, citation-bound advisory service for Indian workplace-compliance questions. It is built around a deliberate hard case: **the Payment of Gratuity Act, 1972 is repealed**, and gratuity now has to be answered from the **Code on Social Security, 2020** as commenced on **21 November 2025**.

The desk answers only from a checked-in corpus of official provisions. Every operative statement must cite a passage that was retrieved in the same run, was in force on the request's `as_of_date`, and lexically supports the claim. When the evidence is incomplete, the fact pattern is contested, or the caller asks for a legal determination, the run is escalated instead of answered.

> This is a cited compliance advisory, not legal advice or a legal opinion. A qualified professional must decide the matter.

---

## What is in the box

- **Multi-agent LangGraph pipeline** — intake → research → draft → verify → (bounded retry) → route.
- **Provision-aware hybrid retrieval** — FAISS inner-product search over deterministic hashed embeddings + BM25, with provision-aware chunking and curated provision fetches.
- **Repeal-aware corpus** — the 1972 Act is retained with `status=repealed`, `effective_to=2025-11-20`, `repealed_by=ssc2020`; retrieval down-weights it, and verification refuses operative claims that rely on it after commencement.
- **Deterministic legal policy** — the model can propose prose and queries, but the legal rule set and routing are deterministic. A model draft is accepted only when every claim is citation-bound and lexically supported.
- **Six-check verification loop** — citation retrieved, in force on date, passage support, numeric consistency, qualifier completeness, no unauthorised determination.
- **Guardrails** — strict schema, synthetic-data attestation (`consent=true`), prompt-injection/toxicity screening, output filtering, body-size limit, quarantine to disk.
- **Durable service layer** — Redis Streams consumer group, at-least-once delivery, bounded retries, dead-letter stream, whole-run cache, per-client token bucket, run TTL.
- **Observability** — Prometheus metrics on API and worker, OpenTelemetry traces exported to Jaeger, structured JSON logs with correlation/run IDs, Grafana dashboard and alert rules.
- **Evaluation and load evidence** — 24-case golden set, v1-vs-v2 matrix, reproducible Locust profiles, one-worker/three-worker scaling, cache measurements, token/cost accounting, and a genuine incident log.
- **Demo UI** — self-contained static page at `/` with demo cases, JSON editor, variant toggle, polling, citation/verification/trace tabs, and feedback.

## Measured results

Golden-set evaluation, 24 synthetic cases, `stub_local`, index `idx-1793004115-b6107331`:

| Metric | v1 single-pass | v2 qualifier-aware |
|---|---:|---:|
| Success rate | 12% | **100%** |
| Route accuracy | 21% | **100%** |
| Verification accuracy | 17% | **100%** |
| Context recall | 52% | **100%** |
| Citation recall | 52% | **100%** |
| Qualifier coverage | 40% | **100%** |
| Hallucination rate | 4% | **0%** |
| Escalations | 23/24 | 4/24 |
| p95 latency | 18.1 ms | 37.3 ms |

Load test through the full Compose stack (Redis Streams + worker + `stub_http`), 4 users for 60 s, declared SLO **p95 ≤ 20,000 ms and ≥ 2 advisory runs/s**:

| Measurement | Result |
|---|---:|
| Advisory runs accepted / completed | 184 / 181 |
| Failures | 0 |
| End-to-end average | 881.8 ms |
| End-to-end p95 | **930 ms** |
| End-to-end max | 1,007.7 ms |
| Throughput | **3.06 runs/s** |
| Queue backlog after drain | 0 |

Scaling and cache on the same Compose stack:

| Measurement | Result |
|---|---:|
| 120 users, 1 worker | p95 **22.0 s**, 4.34 runs/s — SLO fails at the worker/queue |
| 120 users, 3 workers | p95 **8.7 s**, 13.27 runs/s — SLO passes |
| Cache miss / hit p95 | 895.3 ms / **19.9 ms** |
| Cache miss / hit cost per run | $0.000865 / **$0.00** (shadow price) |
| Cost per 100 fresh runs | **$0.0865** at $0.15/$0.60 per Mtok |

The full write-ups are in [`reports/eval_report.md`](reports/eval_report.md), [`reports/load_report.md`](reports/load_report.md), and [`reports/cost_report.md`](reports/cost_report.md). The cost rates are explicit shadow prices for the free deterministic stub, not a provider invoice. These are stub-provider numbers, not a claim about a frontier-model deployment.

## Architecture

```
                        ┌──────────────────────────────┐
   browser / client ───▶│ FastAPI                      │
                        │  ingestion + guardrails      │
                        │  rate limit + run cache      │
                        │  Redis Streams producer      │
                        └──────────────┬───────────────┘
                                       │ compliance:runs
                                       ▼
                        ┌──────────────────────────────┐
                        │ Worker (consumer group)      │
                        │  bounded concurrency         │
                        │  retry → DLQ                 │
                        └──────────────┬───────────────┘
                                       ▼
      ┌─────────────────────────────────────────────────────────────┐
      │ LangGraph                                                   │
      │  intake → research → draft → verify ──retry──▶ research     │
      │                                     └─route─▶ answer/escalate│
      └──────────────┬──────────────────────────────┬───────────────┘
                     │                              │
             ┌───────▼────────┐             ┌───────▼────────┐
             │ Hybrid index   │             │ Redis store    │
             │ FAISS + BM25   │             │ runs, trace,   │
             │ versioned      │             │ feedback, TTL  │
             └───────┬────────┘             └────────────────┘
                     │
             ┌───────▼──────────────────────────────┐
             │ Official corpus (46 provisions)      │
             │ SSC 2020 · POSH 2013 · Gratuity 1972 │
             │ Commencement notifications 2025      │
             └──────────────────────────────────────┘
```

Prometheus scrapes the API (`:8000/metrics`), worker (`:9101/metrics`), and stub provider (`:8080/metrics`). Grafana is provisioned with the Compliance dashboard and Prometheus alert rules. The API and worker export OpenTelemetry traces to Jaeger at http://localhost:16687.

## Quickstart with Docker Compose

Requirements: Docker Engine with Compose v2. The stack builds locally; no API key is required because the default provider is a deterministic stub.

```bash
docker compose up -d --build
```

That starts:

| Service | URL / port | Purpose |
|---|---|---|
| `api` | http://localhost:8000 | HTTP API and demo UI |
| `worker` | internal, metrics `:9101` | Redis Streams consumer |
| `redis` | http://localhost:6379 | runs, cache, rate limits, streams |
| `stub-llm` | http://localhost:8080 | OpenAI-compatible deterministic provider |
| `jaeger` | http://localhost:16687 | OTLP trace backend (UI; API/worker export on the Compose network) |
| `prometheus` | http://localhost:9090 | metrics and alerts |
| `grafana` | http://localhost:3000 (`admin` / `admin`) | provisioned dashboard |

`index-init` builds the index once before the API and worker start. Check readiness:

```bash
curl -fsS http://localhost:8000/health/ready
curl -fsS http://localhost:8000/v1/index
```

Submit a synthetic request and poll it:

```bash
curl -sS -X POST http://localhost:8000/v1/advisories \
  -H 'content-type: application/json' \
  -H 'X-Client-Id: demo-hr' \
  -d '{
    "question": "An employee resigned after 3 years. Is any gratuity payable?",
    "establishment": {"establishment_type": "it_services", "employee_count": 120},
    "employment": {
      "worker_type": "permanent",
      "event": "resignation",
      "completed_service_months": 36,
      "monthly_wage_inr": 45000
    },
    "requester": {"client_id": "demo-hr", "role": "hr"},
    "consent": true
  }'
```

The response is `202` with a `run_id` and `poll_url`:

```bash
curl -sS http://localhost:8000/v1/advisories/<run_id>
curl -sS http://localhost:8000/v1/advisories/<run_id>/trace
```

Open http://localhost:8000 for the demo UI. All demo cases are synthetic.

## Local development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
python -m compliance.cli index build
python -m compliance.cli ask --question "A fixed-term employee's contract expires after 3 years. Is gratuity payable?" \
  --establishment-type it_services --employee-count 120 --worker-type fixed_term --event expiry --months 36 --wage 50000
python -m compliance.cli serve --host 127.0.0.1 --port 8000
```

With no Redis, `QUEUE_BACKEND=auto` falls back to the in-process `InlineQueue`; this is intentional for demos and tests. The production worker **requires** Redis and refuses to start without it.

A `Makefile` wraps the common workflows: `make test`, `make lint`, `make eval`, `make up`, `make smoke`, and `make loadtest`.

### Tests and lint

```bash
PYTHONPATH=src python3 -m pytest -q     # 135 tests
python3 -m ruff check src tests evals loadtest services scripts
```

### Evaluation

```bash
PYTHONPATH=src python3 evals/run_evals.py --variant both
```

This rewrites `reports/eval_report.json`, `reports/eval_report.md`, and `reports/variant_matrix.json`.

### Load test

```bash
locust -f loadtest/locustfile.py --host http://127.0.0.1:8000 \
  --headless --users 4 --spawn-rate 2 --run-time 60s --only-summary \
  --csv reports/loadtest/compose
```

Or with the Compose profile:

```bash
docker compose --profile loadtest build locust
docker compose --profile loadtest run --rm locust
```

See [`loadtest/README.md`](loadtest/README.md) for the profile and the SLO mapping.

## API surface

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/advisories` | Validate, guardrail, rate-limit, cache-check, enqueue |
| `GET` | `/v1/advisories/{run_id}` | Status, progress, result when terminal |
| `GET` | `/v1/advisories/{run_id}/trace` | Ordered agent/tool/verification trace |
| `POST` | `/v1/advisories/{run_id}/feedback` | Caller feedback for a terminal run |
| `GET` | `/v1/feedback/summary` | Aggregate feedback |
| `GET` | `/v1/index` | Index version, corpus hash, embedding config, SLO |
| `GET` | `/v1/prompts` | Prompt registry and hashes |
| `GET` | `/v1/scope` | In-scope topics, exclusions, disclaimer |
| `GET` | `/v1/demo/cases` | Synthetic demo payloads |
| `GET` | `/health/live`, `/health/ready` | Liveness and dependency readiness |
| `GET` | `/metrics` | Prometheus exposition |

Optional request headers: `X-Client-Id` (rate-limit identity) and `X-Prompt-Variant: v1|v2` (A/B evaluation; defaults to `v2`). Full request/response shapes are in [`docs/api.md`](docs/api.md).

## The legal boundary

The corpus contains 46 provisions across four official sources:

| Document | Status | Provisions | As-of |
|---|---|---:|---|
| Code on Social Security, 2020 | in force from 2025-11-21 | 27 | current |
| POSH Act, 2013 | in force | 13 | current |
| Payment of Gratuity Act, 1972 | **repealed** 2025-11-21 | 4 | historical only |
| Commencement notifications, 21 Nov 2025 | in force | 2 | current |

Key rules the desk is built to get right:

- **Gratuity** — Code on Social Security, 2020, s. 53(1): five years of continuous service; the second proviso waives that requirement on death, disablement, or expiry of a fixed-term contract; s. 53(2) read with its third proviso gives the pro-rata rate for fixed-term employees; s. 54 defines 240 days of continuous service; the First Schedule, Chapter V applies the gratuity chapter at the 10-employee threshold.
- **Repeal** — s. 164(1) repeals the Payment of Gratuity Act, 1972. The desk keeps the old Act only so historical `as_of_date` questions (before 21 Nov 2025) are answerable; operative claims under the Code may not cite it.
- **Maternity benefit** — Code on Social Security, 2020, s. 60 and First Schedule, Chapter VI; the 10-employee threshold is in the Schedule.
- **POSH** — s. 4 (Internal Complaints Committee), s. 9 (three-month complaint window, extendable), s. 11 (inquiry), s. 26 (fine up to ₹50,000). POSH was not repealed by the four Labour Codes.
- **Escalation** — an actual harassment allegation, a contested fact pattern, a request for a legal opinion, a prediction of outcome, or an out-of-corpus question is routed to `legal_counsel_review`.

Provenance, source URLs, SHA-256 hashes, and extraction notes are in [`corpus/sources/MANIFEST.json`](corpus/sources/MANIFEST.json) and [`docs/corpus.md`](docs/corpus.md).

## Request contract highlights

- `question` is 10–2,000 characters; `consent` must be `true`.
- `as_of_date` defaults to today and drives the in-force check.
- `employment.completed_service_months` must be consistent with supplied start/end dates (within one month).
- An `expiry` event requires a fixed-term/contract worker type.
- Unknown fields are rejected (`extra="forbid"`).
- Invalid requests are written to `$QUARANTINE_DIR/<date>/<quarantine_id>.json`; **no agent run starts**. In Compose this is the `quarantine-data` volume at `/data/quarantine` (inspect with `docker compose exec api find /data/quarantine -type f`); a host-mode run uses `data/quarantine`. The response is `422` with stable field errors.
- Injection/toxicity hits are quarantined (or sanitised, depending on configuration) before schema validation.

## Repository layout

```
src/compliance/
  api.py                 FastAPI app, routes, quarantine, lifespan
  contracts.py           published request/result schemas
  ingestion.py           single validation + guardrail boundary
  guardrails/            injection, toxicity, output filters
  agents/
    graph.py             LangGraph state machine
    nodes.py             intake, research, draft, verify, route
    policy.py            deterministic legal rules, query plans, routing
    verification.py      six-check claim verification
    state.py             budgets, evidence, trace, usage
  corpus/                chunker, embedder + SQLite cache, hybrid index
  llm/                   local/HTTP clients, circuit breaker, fallback, prompts
  service/               Redis/memory store, cache, limiter, queue, worker
  tools/                 bounded corpus tools (search, lookup, related, currency)
  observability/         metrics and tracing
corpus/                  provisions + official source text + MANIFEST
prompts/                 versioned prompt artifacts and registry
evals/                   golden set and evaluation runner
loadtest/                Locust profile and README
monitoring/              Prometheus rules/config and Grafana provisioning
web/static/              self-contained demo UI
tests/                   unit, contract, integration, golden-matrix tests
docs/                    architecture, corpus, retrieval, guardrails,
                         verification, observability, evals, API, runbook
reports/                 eval, load, cost, cache, observability, demo, and obstacle reports
services/stub_llm.py     deterministic OpenAI-compatible provider
scripts/build_corpus.py  official-text → provisions.jsonl builder
scripts/cache_study.py   cache miss/hit measurement helper
scripts/demo_self_verification.py  bad-draft verification/retry/escalation demo
scripts/demo_circuit_breaker.py    provider failure, breaker open, fallback demo
```

## Documentation

| Document | Contents |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | Components, lifecycle, invariants, failure modes |
| [`docs/corpus.md`](docs/corpus.md) | Sources, provenance, legal facts, repeal mechanics |
| [`docs/retrieval.md`](docs/retrieval.md) | Chunking, hybrid scoring, index versioning, as-of retrieval |
| [`docs/guardrails.md`](docs/guardrails.md) | Ingestion, consent, injection/toxicity, output filter, quarantine |
| [`docs/verification.md`](docs/verification.md) | Claim checks, qualifiers, routing, retry loop |
| [`docs/observability.md`](docs/observability.md) | Metrics, SLOs, dashboard, alerts, logs, traces |
| [`docs/evals.md`](docs/evals.md) | Golden set, metric definitions, variant results |
| [`docs/api.md`](docs/api.md) | Endpoint contracts and examples |
| [`docs/runbook.md`](docs/runbook.md) | Deploy, operate, diagnose, recover |
| [`docs/incident_log.md`](docs/incident_log.md) | Genuine engineering incidents, root causes, fixes |
| [`reports/cost_report.md`](reports/cost_report.md) | Per-step/per-run tokens and cost, cost per 100 runs, largest driver |
| [`reports/observability_evidence.md`](reports/observability_evidence.md) | Jaeger trace, Prometheus targets, alert count, dashboard panels |
| [`reports/demo_evidence.md`](reports/demo_evidence.md) | Ten demonstration checks with run, trace, quarantine, verification, and breaker evidence |
| [`reports/obstacle_report.md`](reports/obstacle_report.md) | Problems found and how they were fixed |

## Known limitations

- The default embedder is deterministic feature hashing. It is fast, local, and reproducible, but it is not a semantic model; `EMBEDDING_PROVIDER=sentence_transformers` is the optional upgrade path.
- The default provider is a stub. Real-model behaviour, cost, and latency must be measured separately before any production claim.
- POSH complaint adjudication, minimum wages, and other topics outside the corpus are deliberately out of scope and escalate.
- The service returns a cited advisory, never a legal opinion, and does not decide contested facts.
- The compose measurements are single-host. The baseline uses one worker; scaling to three workers was measured (p95 22.0 s → 8.7 s at 120 closed-loop users). This is evidence for the declared SLO on this topology, not a capacity plan.
