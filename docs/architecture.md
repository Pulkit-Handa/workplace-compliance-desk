# Architecture

## Goals

The desk has four non-negotiable properties:

1. **No citation, no answer.** An operative statement must be tied to a passage retrieved in the same run.
2. **Currency is checked, not assumed.** Every operative citation is evaluated against the request's `as_of_date`. The repealed 1972 Act is retained but cannot support a post-commencement operative claim.
3. **The model is not the legal authority.** Classification, query plans, claim composition, verification, and routing are deterministic. A model draft is accepted only if every claim is citation-bound and supported by retrieved text.
4. **Incomplete evidence escalates.** Missing qualifiers, a contested complaint, a request for a legal opinion, or an out-of-corpus question routes to `legal_counsel_review`.

## Deployment topology

```
client
  │  POST /v1/advisories
  ▼
FastAPI (api)
  ├─ body-size check
  ├─ schema + consent + guardrails  ──422──▶ quarantine file
  ├─ per-client token bucket        ──429──▶ Retry-After
  ├─ whole-run cache lookup         ──hit──▶ completed envelope
  └─ XADD compliance:runs
                 │
                 ▼
         Redis Streams (consumer group "workers")
                 │  XREADGROUP
                 ▼
         Worker (worker)
           ├─ bounded concurrency (WORKER_CONCURRENCY)
           ├─ LangGraph run
           ├─ persist envelope + trace + request
           ├─ XACK on success
           └─ retry with backoff → dead-letter stream
                 │
                 ├──────────────▶ Redis run store (TTL)
                 └──────────────▶ Prometheus /metrics
```

Clients poll `GET /v1/advisories/{run_id}`. The API and worker share the run store, so a poll may be served by either process. Worker replicas join the same consumer group and take a unique consumer name; no host port is published, so `docker compose up -d --scale worker=3` adds execution capacity without changing the API.

## Components

| Component | File | Responsibility |
|---|---|---|
| API | `src/compliance/api.py` | Routes, lifespan, resources, quarantine, readiness, static UI |
| Ingestion | `src/compliance/ingestion.py` | Single validation boundary; schema, consent, guardrail, size |
| Guardrails | `src/compliance/guardrails/__init__.py` | Injection/toxicity patterns, output filter, disclaimer |
| Graph | `src/compliance/agents/graph.py` | LangGraph nodes, budget failure handling, result assembly |
| Nodes | `src/compliance/agents/nodes.py` | Intake, research, draft, verify, route, retry |
| Policy | `src/compliance/agents/policy.py` | Domain/risk classification, query plans, required/qualifier provisions, claims, routing |
| Verification | `src/compliance/agents/verification.py` | Claim-level checks and issue generation |
| State | `src/compliance/agents/state.py` | Budgets, evidence map, trace, usage, final result |
| Tools | `src/compliance/tools/__init__.py` | Bounded search/lookup/related/currency/coverage tools |
| Index | `src/compliance/corpus/index.py` | Versioned FAISS + BM25 index, chunk lookup, metadata |
| Embedder | `src/compliance/corpus/embedder.py` | Deterministic hashing or optional sentence-transformers; SQLite cache |
| Queue | `src/compliance/service/queue.py` | Redis Streams, inline queue, retry, DLQ, backlog depth |
| Worker | `src/compliance/service/worker.py` | Consumer loop, concurrency, reclaim, graceful shutdown |
| Runner | `src/compliance/service/runner.py` | Shared execution path, cache, envelope/status updates |
| Store | `src/compliance/service/store.py` | Redis and memory run stores, feedback |
| Cache | `src/compliance/service/cache.py` | Whole-run result cache keyed by request + variant + index + prompts + policy fingerprint |
| Limiter | `src/compliance/service/ratelimit.py` | Redis token bucket with local floor |
| LLM | `src/compliance/llm/` | Local deterministic, HTTP, circuit breaker, fallback, prompt registry |

## Request lifecycle

1. **Transport** — FastAPI assigns a correlation ID, enforces a 64 KiB body cap, and attaches request context to logs.
2. **Ingestion** — `validate_payload` screens free text, enforces `consent=true`, validates the strict Pydantic contract, and rejects unknown fields. A rejected request is written to `QUARANTINE_DIR/<YYYY-MM-DD>/<quarantine_id>.json` with a body hash and truncated body. No run is created.
3. **Admission** — the per-client token bucket runs. The cache key includes the normalised payload, prompt variant, prompt artifact hashes, index version, and a fingerprint of the deterministic policy modules. A hit returns `202` with a completed envelope and `cached=true`.
4. **Enqueue** — a queued envelope is persisted, then `XADD` publishes `{run_id, payload, variant, correlation_id}` to `compliance:runs`.
5. **Execution** — the worker moves the run to `running`, executes the graph, and persists the final status and trace. The stream entry is acknowledged only after a successful persist.
6. **Graph**:
   - **intake** — deterministic domain/risk classification; coverage tool; optional model capability probe (never authoritative).
   - **research** — v1 one query; v2 qualifier-aware multi-query plan, curated required-provision fetch, related-provision expansion.
   - **draft** — deterministic policy draft is always composed first as the safe floor. A model draft replaces it only if every claim has a retrieved citation and support ≥ 0.5.
   - **verify** — deterministic six-check verification. On v2 failure, bounded retry (max 2) re-queries using issue suggestions.
   - **route** — deterministic route decision. A model route disagreement is recorded in the trace but cannot override the policy.
7. **Polling** — statuses progress `queued → running → completed | escalated | failed`. Terminal envelopes carry the advisory, citations, claims, verification, route decision, usage, and guardrail report.
8. **Trace** — every step, tool call, model call, verification result, and agent note is appended with sequence number, timestamp, and elapsed milliseconds. `GET .../trace` returns the stored sequence.

## Graph invariants

- The graph is compiled once per process (`build_graph`) and reused.
- Every run has its own `RunContext`, `ToolBox`, and budgets; nothing mutable is shared between callers.
- `MAX_STEPS`, `MAX_TOOL_CALLS`, `MAX_WALL_CLOCK_S`, `MAX_LLM_CALLS`, and `MAX_VERIFY_LOOPS` bound the run. Exceeding a budget produces a failed verification and escalation, not an exception to the caller.
- A re-draft after verification replaces claims and citations; it never appends to the previous draft.
- If a node skips the tail of the graph, defensive result assembly escalates with `incomplete_run`.
- The deterministic policy is authoritative for legal rules and routing. Model disagreement is observable but not controlling.

## Queue semantics

- Redis Streams consumer group (`workers`) with explicit `XACK`.
- Each worker process uses a unique consumer name (`WORKER_CONSUMER_PREFIX-pid`).
- On startup the worker reclaims idle pending entries (`XAUTOCLAIM`) before reading new ones.
- Failures update the run to `retrying`/`dead_lettered`, then retry with exponential backoff up to `QUEUE_MAX_RETRIES`; exhaustion writes to `compliance:runs:dead` and acknowledges the original entry.
- `depth()` is **lag + pending**, not `XLEN`. Acknowledged entries remain in the stream until `MAXLEN` trimming, so `XLEN` would make the backlog alert fire forever. This bug was found and fixed during validation; see `reports/obstacle_report.md`.
- The inline queue used by the API fallback and tests executes handlers as asyncio tasks and keeps the same interface.

## Stores and cache

- **Run store (Redis)** — envelope, trace, normalised request, feedback; TTL `RUN_TTL_S` (7 days by default). The memory store is the fallback for tests and single-process demos.
- **Whole-run cache** — exact-match cache of the completed result. Key includes the normalised request, prompt variant and artifact hashes, index version, and a fingerprint of the deterministic policy modules, so a prompt, corpus, or code-only legal change invalidates naturally. Load tests inject unique `notes` to avoid measuring cache hits; the separate cache study measures the hit path.
- **Index store** — versioned directory (`data/index/indexes/<idx-...>/`) with chunks, vectors, manifest, and `current.json` pointer. The previous two versions are retained for rollback.
- **Embedding cache** — SQLite keyed by embedder fingerprint and text hash, so index rebuilds do not re-embed unchanged chunks.

## Failure modes

| Failure | Behaviour |
|---|---|
| Redis unavailable at API start | `QUEUE_BACKEND=auto` falls back to inline; readiness reflects the selected backends |
| Redis unavailable at worker start | Worker refuses to start (production path has no implicit fallback) |
| Provider timeout/error | Retries with backoff; circuit breaker opens; a degraded fallback draft may be used |
| Tool timeout/budget | Tool returns a failure result or raises budget error; run continues degraded and escalates if evidence is insufficient |
| Index missing/corrupt | Startup/readiness fails with an actionable error; rebuild with `compliance index build` |
| Invalid request | `422`, quarantine file, **no agent run** |
| Verification failure | Withhold advisory, escalate; v2 retries research first up to 2 loops |
| Worker crash mid-run | Pending entry is reclaimed by another consumer after the idle threshold |
| Backlog grows | `compliance_queue_depth` (lag + pending) fires `QueueBacklogGrowing` above 50 for 5 minutes |

## Why deterministic policy plus a model?

A pure model pipeline cannot guarantee that a statutory proviso was retrieved or that a repealed provision was not used as current authority. A pure rules engine cannot write a readable answer or propose useful retrieval queries. The split is deliberate:

- The **model** may classify, propose queries, and write prose.
- The **policy layer** supplies the legal rule set, the required provisions, the claims, and the route.
- The **verifier** accepts or rejects claims mechanically.
- The **router** escalates whenever the mechanical checks do not pass.

This is why the v1 baseline, which retrieves once and does not curate qualifiers, fails most cases while v2 passes. See `docs/evals.md` and `reports/variant_matrix.json`.
