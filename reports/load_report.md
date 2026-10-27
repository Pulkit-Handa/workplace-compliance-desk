# Load test report

## Scope

This report measures the advisory service end to end: HTTP admission, Redis Streams, the worker, the LangGraph pipeline, retrieval, verification, and result polling. It does **not** measure a model-quality claim; the provider is the deterministic stub.

The report covers the baseline topology, a one-worker versus three-worker scaling
comparison, and the whole-run cache effect on the same host:

1. **Compose production baseline** — FastAPI + Redis Streams + one worker + `stub_http`
   provider, 4 users for 60 s.
2. **Compose scaling** — the same workload shape at 120 closed-loop users, first with
   one worker and then with three.
3. **Compose cache** — 10 fresh requests and their exact repeat requests.
4. **Local inline path** — FastAPI with the in-process queue and `stub_local` provider.

The Compose path is the relevant one for the declared SLO. The inline run is a
lower-bound sanity check and a developer convenience, not a deployment claim.

## Declared SLO

The target was declared before measurement and is exposed by `GET /v1/index`:

| Property | Target | Basis |
|---|---|---|
| End-to-end p95 | **≤ 20,000 ms** | `SLO_P95_MS=20000` |
| Sustained throughput | **≥ 2 advisory runs/s** | `SLO_TARGET_RPS=2` |
| Failures | 0 for the measured profile | Acceptance |
| Queue backlog after drain | 0 | Admission control |

The measurement point for latency is **submit → terminal status**, not the POST acknowledge and not an individual poll. Locust records it as the synthetic `ADVISORY / e2e` row.

## Environment

| Property | Value |
|---|---|
| Host | Single Linux workstation, 16 vCPU, 13 GiB RAM |
| Docker | 29.8.2 |
| Runtime image | Python 3.12-slim, non-root UID 10001 |
| API | 1 process, Uvicorn |
| Worker | Baseline 1 replica; scaling test 1 vs 3 replicas, `WORKER_CONCURRENCY=4` each |
| Queue | Redis 7, AOF enabled, consumer group `workers` |
| Provider | `stub_http`, OpenAI-compatible, 150 ms injected delay |
| Embedder | `hashing:v1:384` |
| Index | `idx-1793020189-b6107331`, 82 chunks, 46 provisions |
| Prompt variant | `v2` |
| Date | 2026-10-26 |

No tuning was applied during the run. The container build and index build were completed
first. The scaling runs used the same host, index, provider delay, request shape, and
Locust think time; only the worker replica count changed.

## Workbook

`loadtest/locustfile.py`:

- One synthetic **fixed-term contract expiry** gratuity question.
- Unique `notes` per request, so the whole-run cache cannot produce a cache-only result.
- Unique `X-Client-Id` per simulated user, so each user has its own token bucket.
- Poll every 100 ms; 45 s per-run timeout.
- Wait 0.2–0.6 s between runs.
- A run is failed if admission fails, polling times out, the terminal status is `failed`, or verification did not pass.

Measured Compose profile:

```bash
docker compose --profile loadtest run --rm locust \
  locust -f /app/loadtest/locustfile.py --host http://api:8000 \
  --headless --users 4 --spawn-rate 2 --run-time 60s --only-summary \
  --csv /app/reports/loadtest/compose
```

## Results — Compose production path

4 users, 60 s:

| Measurement | Result |
|---|---:|
| Advisory POSTs accepted | 184 (CSV) |
| End-to-end runs completed | **181** |
| Failures | **0** |
| End-to-end average | 881.8 ms |
| End-to-end median | 900 ms |
| End-to-end p95 | **930 ms** |
| End-to-end p99 | 960 ms |
| End-to-end max | 1,007.7 ms |
| End-to-end throughput | **3.06 runs/s** |
| Poll GETs | 1,586 |
| Poll GET p95 | 14 ms |
| Aggregate requests | 1,951 |
| Aggregate failure rate | 0% |

SLO assessment:

- Latency: **930 ms p95 versus 20,000 ms target — 21.5× headroom.**
- Throughput: **3.06 runs/s versus 2.0 target — 1.53× headroom.**
- Errors: **0 / 1,951 requests; 0 / 181 completed runs.**
- Backlog after drain: see below.

The cache was deliberately bypassed by unique notes, so the throughput is not inflated by repeated-request caching. Admission is not the bottleneck: POST p95 was 27 ms while end-to-end p95 was 930 ms. Most of the time is the advisory graph and the 150 ms provider delay, not HTTP or Redis.

### Queue drain evidence

At the time of the measured run, after the worker drained:

```text
XLEN compliance:runs                          322
XPENDING compliance:runs workers              0
XINFO GROUPS compliance:runs  lag             0
compliance_queue_depth                        0
compliance_queue_pending_entries              0
compliance_active_runs                        0
XLEN compliance:runs:dead                     0
```

The 322 retained stream entries include earlier smoke and load runs; acknowledged entries are not removed until trimming. The backlog is **lag + pending = 0**, which is the number the gauge uses. The dead-letter stream is empty.

## Results — local inline path

5 users, 20 s, `QUEUE_BACKEND=inline`, `stub_local`:

| Measurement | Result |
|---|---:|
| Advisory POSTs accepted | 164 |
| End-to-end runs completed | **163** |
| Failures | 0 |
| End-to-end average | 170.2 ms |
| End-to-end median | 160 ms |
| End-to-end p95 | **300 ms** |
| End-to-end max | 353.9 ms |
| Throughput | **8.57 runs/s** |
| Aggregate requests | 667 |

The inline path removes the Redis hop and the synthetic network provider. It is faster, but it also removes the durability and cross-process delivery that the production path is designed to provide. It is not the SLO evidence.

## Scaling — one worker versus three workers

Profile: **120 closed-loop users, 30 s, unique notes**, same host and provider. Each
simulated user waits for the terminal status before its next run, so 120 users is not 120
simultaneous server-side requests; it is the client concurrency that exposed the
saturation point.

| Measurement | 1 worker | 3 workers | Change |
|---|---:|---:|---:|
| Advisory POSTs accepted | 242 | 494 | — |
| End-to-end runs completed in window | 126 | 386 | — |
| Failures | 0 | 0 | — |
| End-to-end median | 13,000 ms | 7,300 ms | -43.8% |
| End-to-end p95 | **22,000 ms** | **8,700 ms** | **-60.5%** |
| End-to-end average | 12,551.9 ms | 6,579.1 ms | -47.6% |
| End-to-end max | 23,540.1 ms | 9,135.3 ms | -61.2% |
| End-to-end throughput | **4.34 runs/s** | **13.27 runs/s** | **+206%** |
| POST p95 | 1,500 ms | 1,500 ms | unchanged |
| Not terminal when the 30 s window closed | 116 | 108 | — |

At 120 users the one-worker topology **breaches the declared p95 SLO**: 22.0 s versus
20.0 s. The POST path is still fast (1.5 s p95), so admission is not the bottleneck; the
worker therefore is accepting work faster than a single four-slot worker can execute it.
The queue backlog drains after the load stops.

The failure point is the **worker / Redis Streams execution stage**, not the API, index,
or provider health. Adding two workers (1 → 3, 3.0×) increases completed throughput
by 3.06× and cuts p95 by 60.5%, moving the same 120-user profile back inside the SLO
with 11.3 s of headroom. The p95 is still above the 930 ms baseline because the
closed-loop users keep the three workers busy, not because the system is unhealthy.

The runs that had not reached terminal status at the 30 s cutoff are not failures. They
are work accepted inside the window but still queued when Locust stopped. The queue was
confirmed at **lag 0, pending 0** after the run; the retained `XLEN` is not backlog.

Reproduce with:

```bash
docker compose up -d --scale worker=1

docker compose --profile loadtest run --rm --no-deps --entrypoint locust locust \
  -f /app/loadtest/locustfile.py --host http://api:8000 --headless \
  --users 120 --spawn-rate 20 --run-time 30s --only-summary \
  --csv /app/reports/loadtest/scale_1w_120

docker compose up -d --scale worker=3

docker compose --profile loadtest run --rm --no-deps --entrypoint locust locust \
  -f /app/loadtest/locustfile.py --host http://api:8000 --headless \
  --users 120 --spawn-rate 20 --run-time 30s --only-summary \
  --csv /app/reports/loadtest/scale_3w_120
```

## Cache effect

Profile: 10 synthetic gratuity requests, each submitted twice. The first submission is a
cache miss (worker + four provider calls); the second is the identical request and hits
the whole-run cache before enqueueing. Measurements and the raw records are in
[`reports/cache_study.json`](cache_study.json); cost is broken down in
[`reports/cost_report.md`](cost_report.md).

| Measurement | Cache miss | Cache hit | Change |
|---|---:|---:|---:|
| Samples | 10 | 10 | — |
| p50 wall time | 820.5 ms | 17.3 ms | -97.9% |
| p95 wall time | **895.3 ms** | **19.9 ms** | **-97.8%** |
| Provider calls | 4 per run | 0 incremental | -100% |
| Tokens charged | 5,749.6 prompt + 4 completion per run | 0 incremental | -100% |
| Cost per run | $0.00086484 | **$0.00** | -100% |
| Cost per 100 runs | $0.086484 | **$0.00** | -100% |

The hit removes almost all of the end-to-end latency and all of the incremental token
cost. A cached response echoes the original usage metadata for audit; the API does not
call the provider again. The cache is deliberately keyed to the request, variant, index,
prompt hashes, and a fingerprint of the deterministic policy modules, so a code-only
legal change invalidates old entries.

## Interpretation

- The service is **admission-bound only under much heavier load** than this profile; at 4 concurrent users, the POST path is comfortably under 30 ms p95.
- The advisory pipeline dominates end-to-end latency. The worker concurrency of 4 is sufficient for this load, and the queue never accumulated a sustained backlog.
- Throughput is limited by the worker and provider in this topology. The observed 3.06 runs/s is not a capacity ceiling; it is the rate achieved while 4 users were kept busy by ~0.9 s runs.
- The 45 s per-run timeout was never approached. The maximum observed run was 1.0 s.
- A real provider will change these numbers. The correct next measurement is the same profile against the intended real endpoint, with token cost and provider-side rate limits included.

## SLO mapping

Baseline (4 users / 60 s, one worker):

| SLO | Measured | Verdict |
|---|---|---|
| p95 ≤ 20,000 ms | 930 ms | Pass, 21.5× headroom |
| ≥ 2 runs/s | 3.06 runs/s | Pass, 1.53× headroom |
| 0 failures | 0 | Pass |
| Queue backlog 0 after drain | 0 | Pass |

Stress (120 users / 30 s):

| SLO | 1 worker | 3 workers | Verdict |
|---|---:|---:|---|
| p95 ≤ 20,000 ms | **22,000 ms** | **8,700 ms** | Fails at 1 worker; passes at 3 |
| ≥ 2 runs/s | 4.34 runs/s | 13.27 runs/s | Pass in both; 3-worker headroom 6.6× |
| 0 failures | 0 | 0 | Pass |
| Queue backlog 0 after drain | 0 | 0 | Pass after drain in both |

The pass is for **this topology and this stub provider**. It is not evidence that a
frontier-model deployment meets 20 s p95 at 2 runs/s. The stress result does identify the
failure concurrency and the component: at **120 closed-loop users**, a single
four-concurrency worker is the bottleneck. Scaling workers restores the SLO.

## Reproducibility

1. Build and start the stack:

   ```bash
   docker compose up -d --build
   curl -fsS http://localhost:8000/health/ready
   ```

2. Confirm the index and provider:

   ```bash
   curl -fsS http://localhost:8000/v1/index | jq '{index_version, chunk_count, provision_count}'
   curl -fsS http://localhost:8080/health
   ```

3. Run the profile (4 users / 60 s) and write CSV to the checked-in report directory.

4. Confirm the drain:

   ```bash
   docker compose exec -T redis redis-cli XINFO GROUPS compliance:runs
   docker compose exec -T worker curl -fsS http://127.0.0.1:9101/metrics | grep '^compliance_queue_depth'
   ```

The checked-in CSVs are:

- `reports/loadtest/compose_stats.csv`
- `reports/loadtest/compose_stats_history.csv`
- `reports/loadtest/compose_failures.csv`
- `reports/loadtest/compose_exceptions.csv`
- `reports/loadtest/inline_stats.csv`
- `reports/loadtest/inline_failures.csv`
- `reports/loadtest/inline_exceptions.csv`
- `reports/loadtest/scale_1w_120_*.csv`
- `reports/loadtest/scale_3w_120_*.csv`
- `reports/loadtest/scale_1w_60_*.csv` (intermediate probe)
- `reports/loadtest/scale_1w_{12,20,30}_*.csv` (intermediate probes)

## Caveats and anomalies

- **POST count discrepancy.** The Locust console printed 185 POSTs; the final CSV contains 184. The difference is one submission at the teardown edge, and the CSV is treated as authoritative in this report. The end-to-end count is 181 in both.
- **181 vs 184.** Three accepted runs had not reached a terminal state before the 60 s window closed. They are not failures and are not included in the e2e latency sample.
- **One host.** The API, worker, Redis, and provider share a machine. Network latency between services is loopback, not a real network.
- **Stub provider.** The 150 ms delay is synthetic. A real provider's queueing, retries, token limits, and cost are not represented.
- **Workload breadth.** One question shape exercises the legal path but not every domain. It is a load test, not a quality benchmark.
- **No authentication or TLS.** The demo API has no auth; those layers would add latency and failure modes in a real deployment.
- **Cache disabled in the load runs by design.** Unique notes prevent cache hits, so the baseline and scaling numbers measure the worker path. The separate cache study measures the hit path: p95 895.3 ms → 19.9 ms.
- **Index build excluded.** The index was built before the run; cold-start index construction is not in these numbers.

## Bottom line

On a single host with the deterministic provider, the baseline full Compose path sustained
**3.06 completed advisories per second at 930 ms p95 with zero failures**, and drained to
**zero backlog**. At 120 closed-loop users the one-worker topology reached its limit:
**p95 22.0 s, 4.34 runs/s**, with the worker/queue as the bottleneck. Three workers kept
the same profile inside the SLO at **p95 8.7 s and 13.27 runs/s**. The whole-run cache
reduced repeated-request p95 from **895.3 ms to 19.9 ms** and removed the incremental
provider cost. These are evidence for this topology and this stub provider; a real model
and a multi-host deployment must be measured on their own terms.
