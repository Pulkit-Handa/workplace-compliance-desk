# Runbook

## Start and stop

```bash
# start the full stack
docker compose up -d --build

# status and logs
docker compose ps
docker compose logs -f api worker

# stop, keeping data volumes
docker compose down

# stop and delete volumes (destroys runs, index, feedback)
docker compose down -v
```

Startup order is enforced by Compose: `redis` healthy → `index-init` completes → `stub-llm` healthy → `api` and `worker`. The API waits for readiness; the worker requires Redis and refuses to start without it.

`index-init` runs `python -m compliance.cli index build`. It is idempotent: an unchanged corpus/chunking/embedding configuration reuses the current index; a change produces a new version.

## Readiness and smoke checks

```bash
curl -fsS http://localhost:8000/health/live
curl -fsS http://localhost:8000/health/ready
curl -fsS http://localhost:8000/v1/index | jq '{index_version, chunk_count, provision_count, corpus_hash}'
curl -fsS http://localhost:8000/v1/prompts | jq '.active_variant'
curl -fsS http://localhost:8000/metrics | grep -E '^compliance_(queue_depth|active_runs|runs_total)'
docker compose exec -T worker curl -fsS http://127.0.0.1:9101/metrics | grep -E '^compliance_(queue_depth|queue_pending|active_runs|runs_total)'
curl -fsS http://localhost:16687/api/services
```

`jaeger` is a single-node trace backend; `compliance-api` should appear in
`/api/services` after a request. Open http://localhost:16687 for traces. The app exports
OTLP over the Compose network, so the worker can scale without port conflicts.

Expected: readiness `status: ok`, all checks `true`, queue depth `0` when idle.

End-to-end smoke:

```bash
curl -sS -X POST http://localhost:8000/v1/advisories \
  -H 'content-type: application/json' \
  -d '{
    "question": "An employee resigned after 3 years. Is any gratuity payable?",
    "establishment": {"establishment_type": "it_services", "employee_count": 120},
    "employment": {"worker_type": "permanent", "event": "resignation", "completed_service_months": 36, "monthly_wage_inr": 45000},
    "requester": {"client_id": "runbook", "role": "hr"},
    "consent": true
  }' | jq .
```

Then poll, and check the trace:

```bash
curl -sS http://localhost:8000/v1/advisories/<run_id> | jq '{status, route: .result.route, verification: .result.verification.outcome}'
curl -sS http://localhost:8000/v1/advisories/<run_id>/trace | jq '.events[] | {seq, agent, name, kind}'
```

A correct current gratuity answer routes `answer_with_citation` and verifies `passed`. A complaint or out-of-scope question routes `legal_counsel_review`.

## Configuration

Settings are environment variables (`.env` or Compose). No secret has a default. Important groups:

| Setting | Default | Notes |
|---|---|---|
| `PROMPT_VARIANT` | `v2` | `v1` is the baseline; can be overridden per request with `X-Prompt-Variant` |
| `EMBEDDING_PROVIDER` | `hashing` | `sentence_transformers` needs the `ml` extra and a model |
| `LLM_PROVIDER` | `stub_local` | Compose uses `stub_http`; production would use `openai_compatible` |
| `LLM_BASE_URL`, `LLM_MODEL` | empty | Required for `openai_compatible` |
| `LLM_API_KEY` | empty | Never commit |
| `QUEUE_BACKEND` | `auto` | Compose uses `redis`; `auto` falls back inline only for the API |
| `WORKER_CONCURRENCY` | `1` | Compose sets `4` |
| `QUEUE_MAX_RETRIES` | `2` | Retries before dead-letter |
| `RATE_LIMIT_PER_MINUTE` / `BURST` | 60 / 10 | Compose uses 600 / 60 |
| `RUN_TTL_S` | 604800 | 7 days |
| `MAX_VERIFY_LOOPS` | 2 | Three verification attempts total |
| `SLO_P95_MS` / `SLO_TARGET_RPS` | 20000 / 2.0 | Declared before measurement |

After changing configuration, restart the affected service:

```bash
docker compose up -d --force-recreate api worker
```

Changing prompt artifacts, chunking, or the deterministic policy composer/verifier requires new hashes; restart all app services so caches and prompt registries reload. The whole-run cache key includes a fingerprint of the policy modules, so a code-only legal change invalidates old entries once the process restarts.

## Index operations

```bash
# inspect the active index
python3 -m compliance.cli index info

# rebuild (reuses unchanged vectors)
python3 -m compliance.cli index build

# force a new version even if inputs look unchanged
python3 -m compliance.cli index build --force
```

In Compose:

```bash
docker compose run --rm index-init
# or
docker compose exec api python -m compliance.cli index build --force
```

The index lives in the `index-data` volume. Previous two versions are retained for rollback; `current.json` is the pointer. If the active index is corrupt or missing:

1. Check `docker compose exec api python -m compliance.cli index info`.
2. Rebuild from the checked-in corpus.
3. Restart `api` and `worker` so both load the new version.
4. Confirm `/v1/index` and `/health/ready`.

An embedding fingerprint mismatch is a hard failure by design; rebuild with the configured provider rather than mixing vector spaces.

## Queue operations

Keys:

| Key | Meaning |
|---|---|
| `compliance:runs` | Redis Stream of run entries |
| `compliance:runs:dead` | Dead-letter stream |
| `compliance:run:<run_id>` | Run envelope, trace, request |
| `compliance:feedback` | Feedback list (last 1,000) |

Inspect:

```bash
docker compose exec redis redis-cli XLEN compliance:runs
docker compose exec redis redis-cli XPENDING compliance:runs workers
docker compose exec redis redis-cli XINFO GROUPS compliance:runs
docker compose exec redis redis-cli XLEN compliance:runs:dead
```

**Do not use `XLEN` as the backlog.** Acknowledged entries remain in the stream until trimming. The correct backlog is:

```
lag + pending
```

which is what `compliance_queue_depth` reports. `XLEN` can be large while the backlog is zero; that is normal.

### Backlog growing

1. Check `compliance_queue_depth`, `compliance_queue_pending_entries`, and `compliance_active_runs` on the worker.
2. Check `docker compose logs --tail=200 worker` for repeated failures or slow provider calls.
3. Check `compliance_run_duration_seconds` p95 and `compliance_provider_failures_total`.
4. If the worker is healthy but under-provisioned, increase `WORKER_CONCURRENCY` or add worker replicas (each process gets a unique consumer name).
5. If entries are stuck pending, check the worker crash/restart history; `XAUTOCLAIM` reclaims idle entries on startup.
6. Do not delete the stream to “fix” a backlog; that loses work.

### Dead letters

```bash
docker compose exec redis redis-cli XRANGE compliance:runs:dead - + COUNT 5
```

Each DLQ entry contains `run_id`, `payload`, `attempt`, `error`, and `dead_at`. The run store also has `status=failed` and an error message. To replay manually:

1. Read the entry.
2. Correct the underlying cause (provider, index, bug).
3. Re-enqueue the payload with `XADD compliance:runs * run_id <id> payload '<json>' attempt 0`.
4. Or re-submit the request through the API if it is still valid.

There is no automatic DLQ replay, by design.

### Drain before maintenance

```bash
while [ "$(docker compose exec -T redis redis-cli XINFO GROUPS compliance:runs | awk '/lag/{print $2}')" != "0" ]; do sleep 2; done
```

Then `docker compose stop api` (stop accepting) and let the worker finish before restarting. Compose sets a 30-second stop grace period.

### Scale workers

```bash
# scale out
docker compose up -d --scale worker=3

# confirm replicas and backlog
docker compose ps --format 'table {{.Service}}\t{{.Name}}\t{{.Status}}' | grep worker
docker compose exec -T redis redis-cli XINFO GROUPS compliance:runs

# scale back
docker compose up -d --scale worker=1
```

The worker has no host port mapping; metrics stay on the Compose network at
`worker:9101`, which Prometheus scrapes. To inspect a replica, use
`docker compose exec -T worker ...` when there is one, or target a container id with
`docker exec <id> ...` when scaled.

Measured effect (120 closed-loop users, 30 s): one worker p95 **22.0 s**, 4.34 runs/s;
three workers p95 **8.7 s**, 13.27 runs/s. The one-worker limit is the worker/queue stage,
not API admission. See [`reports/load_report.md`](../reports/load_report.md).

## Cache and cost

The whole-run cache key includes the request, variant, index, prompt hashes, and a
fingerprint of the policy modules. A cache hit returns before enqueueing. To measure it:

```bash
python3 scripts/cache_study.py --output reports/cache_study.json
```

Cost is reported per step and per run in `result.usage` and in Prometheus. The Compose
profile uses explicit shadow prices (`LLM_INPUT_COST_PER_MTOK=0.15`,
`LLM_OUTPUT_COST_PER_MTOK=0.60`) because the stub provider has no invoice. See
[`reports/cost_report.md`](../reports/cost_report.md).

## Provider failover

`stub_local` → deterministic in-process. `stub_http` → OpenAI-compatible endpoint. `openai_compatible` → a real endpoint.

Health:

```bash
curl -fsS http://localhost:8080/health
curl -fsS http://localhost:8080/metrics | grep -E 'stub_llm'
```

If the provider is failing:

1. `compliance_provider_failures_total` rises and `compliance_circuit_breaker_state` changes.
2. The circuit breaker opens after `CIRCUIT_BREAKER_THRESHOLD` failures and cools down for `CIRCUIT_BREAKER_COOLDOWN_S`.
3. The run may use a degraded fallback and is marked `degraded=true`.
4. Verification still runs; a degraded run that cannot be verified escalates.

To fail over:

- Point `LLM_BASE_URL` / `LLM_MODEL` at the secondary endpoint, or
- Set `LLM_PROVIDER=stub_local` for a safe deterministic fallback during an incident.

Restart `api` and `worker` after changing provider settings. Never disable verification to “keep the service up”.

The deterministic proof is in `tests/test_resilience.py`:

- two failures open the breaker;
- an open breaker rejects a new call;
- one half-open probe is allowed, then success closes the breaker;
- the fallback path returns a result with `fallback_used=true` and the original
  error, rather than pretending the primary call succeeded.

For a live demonstration, stop the stub provider:

```bash
docker compose stop stub-llm
# submit a request; the run uses the local deterministic fallback and is degraded
docker compose start stub-llm
```

To see the breaker itself open quickly, set `CIRCUIT_BREAKER_THRESHOLD=1` and
`CIRCUIT_BREAKER_COOLDOWN_S=10` in the app environment for the demo, then inspect
`compliance_circuit_breaker_state`. Restore the defaults afterwards.

## Common incidents

The dated incident history, root causes, and regression evidence are in
[`docs/incident_log.md`](incident_log.md).

### All requests `422`

- Check `field_errors` and the quarantine file.
- `consent` missing or false is the most common cause.
- A recent schema change may have made an old client payload invalid; unknown fields are rejected.
- Guardrail findings identify the matched field and pattern.

### Requests accepted but never complete

1. `docker compose ps` — is the worker up?
2. `docker compose logs --tail=200 worker` — `queue_read_failed`, `worker_run_failed`, provider timeouts.
3. `compliance_queue_depth` > 0 and `compliance_active_runs` 0 → worker is not consuming; check Redis connectivity and consumer group.
4. `compliance_active_runs` at `WORKER_CONCURRENCY` → runs are slow; inspect p95 and provider latency.

### Verification failure rate spikes

- `compliance_verification_failures_total` by reason.
- `qualifier_missing` → retrieval or policy regression; check the index version and prompt variant.
- `not_in_force_on_date` → corpus status/effective-date bug or wrong `as_of_date`.
- `number_not_supported` → model invented a threshold/rate; inspect the draft.
- `weak_passage_support` → retrieval returned a topically related but non-supporting passage.

Do not lower the thresholds to clear the alert. Fix retrieval or the draft.

### Queue alert fires but the queue is empty

`compliance_queue_depth` should be 0. If it is not, inspect `XINFO GROUPS`. The implementation uses `lag + pending`; if lag is unavailable, it falls back to `XLEN - pending`. A large `XLEN` alone is not an incident.

### Grafana/Prometheus cannot start

On SELinux hosts, bind mounts need a label:

```yaml
- ./monitoring/prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro,z
```

Without `:z`/`:ro,z`, the container gets permission denied and Prometheus exits. This was encountered and fixed in the Compose stack; see `reports/obstacle_report.md`.

### Quarantine volume grows

- `compliance_quarantine_total{reason=...}` identifies the cause.
- Inspect `docker compose exec api find /data/quarantine -type f` (or the host path
  `$QUARANTINE_DIR/<date>/` in host mode).
- Quarantine files include caller-supplied synthetic text. Apply retention and access control; do not copy them into tickets.
- A quarantine spike is often a client bug (missing `consent`, unknown field) rather than an attack.

## Backup and restore

| Data | Location | Backup |
|---|---|---|
| Runs, trace, feedback | Redis `redis-data` volume | `BGSAVE` / snapshot the volume |
| Index | `index-data` volume | Rebuildable from corpus; snapshot for fast recovery |
| Quarantine | `quarantine-data` volume or bind mount | Archive per retention policy |
| Prometheus TSDB | `prometheus-data` | Optional; not required for correctness |
| Grafana | `grafana-data` | provisioned dashboards are in git; DB is optional |

Run store and feedback expire after `RUN_TTL_S`. If records must be retained longer, export them before expiry.

## Security notes

- No secrets in the repository; use environment/secret injection.
- Run the container as the non-root `compliance` user (UID 10001).
- Do not expose Redis or the stub provider publicly; Compose publishes them for local development only.
- The API has no authentication in this demo. Put it behind an authenticated gateway before any real use, and scope `X-Client-Id` to a verified identity.
- Quarantine files contain request text. Treat them as sensitive even though callers attest to synthetic data.
- TLS termination is not included; terminate at a reverse proxy.

## Maintenance checklist

- [ ] `pytest` green.
- [ ] `ruff` clean.
- [ ] Golden eval regenerated and diff reviewed.
- [ ] Load test re-run if the request path or worker topology changed.
- [ ] Index rebuilt if corpus, chunking, or embeddings changed.
- [ ] `/v1/index` reports the expected version and corpus hash.
- [ ] Prometheus targets `up`; Grafana dashboard loaded.
- [ ] Queue depth 0 after drain; no DLQ growth.
- [ ] Quarantine volume and retention checked.
