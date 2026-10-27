# Load test

The profile measures the **whole advisory path**, not just the HTTP accept call:

```
POST /v1/advisories  ──▶ Redis Stream ──▶ worker ──▶ LangGraph
                                                       │
                        poll GET /v1/advisories/:id ◀──┘
                                 until completed / escalated / failed
```

Locust records two things:

| Row | Meaning |
|---|---|
| `POST /v1/advisories` | Admission latency: validation, guardrail, rate limit, cache lookup, enqueue |
| `GET /v1/advisories/{run_id}` | Poll latency |
| `ADVISORY / e2e` | Submit-to-terminal latency, recorded once per completed run |

The SLO is compared against the **`ADVISORY / e2e`** row, not the individual HTTP rows.

## Profile

`loadtest/locustfile.py` simulates an HR user:

- one synthetic fixed-term-expiry gratuity question;
- `X-Prompt-Variant: v2`;
- a unique `notes` value per request, so the whole-run cache cannot turn the test into a cache benchmark;
- a unique `X-Client-Id` per simulated user, so the per-client token bucket is exercised rather than shared;
- polling every 100 ms, with a 45 s per-run timeout;
- wait time of 0.2–0.6 s between runs.

A run is considered failed if the POST is not `200`/`202`, if polling does not reach a terminal state in time, if the terminal status is `failed`, or if a completed result did not pass verification.

## Declared SLO

Declared before the measurement (also exposed by `GET /v1/index`):

| Property | Target |
|---|---|
| End-to-end p95 | ≤ 20,000 ms |
| Sustained throughput | ≥ 2 advisory runs/s |
| Failures | 0 for the measured profile |
| Queue backlog after drain | 0 |

## Running against Compose

The Compose profile defaults to **20 users for 90 s**:

```bash
docker compose --profile loadtest build locust
docker compose --profile loadtest run --rm locust
```

That writes `/app/reports/loadtest/locust_*.csv` on the host through the bind mount. To reproduce the checked-in measured profile instead:

```bash
docker compose --profile loadtest run --rm locust \
  locust -f /app/loadtest/locustfile.py --host http://api:8000 \
  --headless --users 4 --spawn-rate 2 --run-time 60s --only-summary \
  --csv /app/reports/loadtest/compose
```

The measured Compose run used `api`, `worker` (concurrency 4), `redis`, and `stub-llm`
with a 150 ms synthetic delay.

Scaling profile (120 closed-loop users, 30 s):

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

The load-test service runs as the invoking host user (`${LOCUST_UID:-1000}`) so the CSV
bind mount is writable. Override `LOCUST_UID`/`LOCUST_GID` if the host user is different.

## Running against a local API

With the API already on `http://127.0.0.1:8000` (Redis-backed or inline):

```bash
locust -f loadtest/locustfile.py --host http://127.0.0.1:8000 \
  --headless --users 5 --spawn-rate 2 --run-time 20s --only-summary \
  --csv reports/loadtest/inline
```

For the inline measurement the API was started with `QUEUE_BACKEND=inline` and `LLM_PROVIDER=stub_local`. The inline queue is a demo/test fallback, not the production service path.

## Results

Checked-in evidence:

- [`reports/loadtest/compose_stats.csv`](../reports/loadtest/compose_stats.csv) — full Compose stack, 4 users / 60 s.
- [`reports/loadtest/inline_stats.csv`](../reports/loadtest/inline_stats.csv) — local inline API, 5 users / 20 s.
- [`reports/loadtest/scale_1w_120_stats.csv`](../reports/loadtest/scale_1w_120_stats.csv) — 120 users, one worker.
- [`reports/loadtest/scale_3w_120_stats.csv`](../reports/loadtest/scale_3w_120_stats.csv) — 120 users, three workers.
- [`reports/loadtest/scale_1w_60_stats.csv`](../reports/loadtest/scale_1w_60_stats.csv) — intermediate 60-user probe.
- `reports/loadtest/scale_1w_{12,20,30}_stats.csv` — exploratory concurrency probes, retained for traceability but not used in the headline scale comparison.
- [`reports/load_report.md`](../reports/load_report.md) — interpretation, caveats, SLO mapping, and cache evidence.
- [`reports/cache_study.json`](../reports/cache_study.json) — raw cache miss/hit records.

Headline:

| Topology | Accepted | E2E completed | Failures | E2E p95 | E2E throughput |
|---|---:|---:|---:|---:|---:|
| Compose baseline (Redis + 1 worker + `stub_http`) | 184 (CSV) | 181 | 0 | 930 ms | 3.06 runs/s |
| Compose stress, 1 worker | 242 | 126 | 0 | **22,000 ms** | 4.34 runs/s |
| Compose stress, 3 workers | 494 | 386 | 0 | **8,700 ms** | 13.27 runs/s |
| Local inline (`stub_local`) | 164 | 163 | 0 | 300 ms | 8.57 runs/s |

`ADVISORY / e2e` row counts are always lower than POST counts when a submission is still in flight at the end of the run window.

## Reading the CSV

Locust's `*_stats.csv` has one row per request name plus an `Aggregated` row. Use:

```bash
column -s, -t reports/loadtest/compose_stats.csv
```

Columns used in the report:

- `Request Count`, `Failure Count`
- `Average Response Time`, `50%`, `95%`, `100%`
- `Requests/s`
- the custom `ADVISORY / e2e` row for end-to-end numbers

`compose_failures.csv` and `inline_failures.csv` contain only the header when there were no failures.

## What this does not prove

- It is a **single-host** measurement. The baseline uses one worker; the 120-user profile
  was measured with one and three workers.
- The provider is a deterministic stub with an injected delay. Real-model latency, cost,
  token usage, and failure modes are different.
- The workload is one fixed-term gratuity question. It exercises the full legal path but
  not the full breadth of questions.
- The load runs deliberately defeat the cache with unique `notes`; the separate
  [`reports/cache_study.json`](../reports/cache_study.json) study measures the hit path.
- The token bucket is sized for this profile (600/min, burst 60) and each user gets its
  own identity.
- It is evidence for this topology, not a capacity plan or a claim about a frontier-model
  deployment.
