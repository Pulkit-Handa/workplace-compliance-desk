# Obstacle report

This project had a deliberately awkward legal starting point and a normal set of engineering failures. The table below is the short version; the sections after it explain what went wrong, how it was found, and what prevents a regression. The dated operational record is in [`docs/incident_log.md`](../docs/incident_log.md).

| # | Obstacle | Class | Resolution | Evidence |
|---|---|---|---|---|
| 1 | Repealed Act still retrievable beside its successor | Legal / retrieval design | Status-aware ranking; as-of retrieval; verification rejects operative repealed citations | `test_repealed_act_is_marked…`, `test_toolbox_currency…`, 1972 corpus entries |
| 2 | Provisos merged into the main section | Retrieval correctness | Provision-aware chunking; provisos stored and chunked separately | `test_proviso_is_a_separate_provision` |
| 3 | Embedding hash ignored configured dimension | Correctness bug | `_stable_hash(feature, dim)`; dimension included in fingerprint | `test_hashing_embedder…`, 32/64-dim tests plus the 384-dim production build |
| 4 | Queue depth reported retained stream length | Observability bug | `depth = lag + pending`, with `XLEN - pending` fallback | `test_redis_queue_depth_uses_lag_plus_pending…` |
| 5 | Docker built the `loadtest` stage by default | Packaging bug | `target: runtime` on app services; `loadtest` separate | image contains no `pytest`/`locust` |
| 6 | SELinux denied Prometheus/Grafana bind mounts | Deployment bug | `:ro,z` / `:z` labels | Compose stack running |
| 7 | API shutdown called `close` on an `aclose`-only client | Runtime bug | `_close_resource` helper; regression test | `test_shutdown_closes_llm_clients_that_use_aclose` |
| 8 | Load test could become a cache/rate-limit benchmark | Measurement trap | Unique notes + per-user client ids | `loadtest/locustfile.py` |
| 9 | Locust console and CSV counts differed by one | Measurement anomaly | CSV treated as authoritative; documented | `reports/load_report.md` |
| 10 | Whole-run cache survived a code-only policy change | Caching bug | Cache key now includes a policy-module fingerprint | `test_cache_key_covers_every_input…` |
| 11 | Below-threshold answer still showed a gratuity calculation | Legal-output quality | Rate/arithmetic/forfeiture skipped when the five-year threshold is unmet | `test_below_threshold_case…` |
| 12 | Required-provision fetch accepted the first chunk of a multi-chunk section | Retrieval correctness | Fetch the whole provision when any chunk is missing; citation selector chooses the best chunk | `maternity_creche_50` golden case, 24-case v2 100% |
| 13 | Host-published worker metrics port blocked worker scale-out | Deployment bug | Worker now uses `expose: ["9101"]`; no host mapping | 1-worker vs 3-worker load runs |
| 14 | Jaeger host OTLP port collided with another local stack | Deployment collision | Publish UI only on `16687`; keep OTLP internal on `jaeger:4318` | `GET :16687/api/services` → `compliance-api` |

## 1. The repealed Act cannot simply be deleted

### Problem

The Payment of Gratuity Act, 1972 is repealed by the Code on Social Security, 2020, s. 164(1), with effect from **21 November 2025**. The obvious move — remove the old Act from the corpus — would make the service unable to answer historical questions with an `as_of_date` before commencement. Keeping it without special handling would let a current answer cite repealed law.

### Resolution

The corpus keeps both regimes:

- the 1972 Act has `status=repealed`, `effective_to=2025-11-20`, `repealed_by=ssc2020`;
- the 2020 Code and the commencement notification have `effective_from=2025-11-21`;
- retrieval applies a ranking penalty to repealed material but does not hard-exclude it;
- the query plan selects the historical branch for `as_of_date < 2025-11-21`;
- `check_currency` reports the status and the successor;
- verification independently checks `effective_from <= as_of <= effective_to` for every operative citation, and rejects a claim that mixes repealed and in-force authority.

### Why it matters

A status field alone is not enough. The verifier is what makes the status operationally binding. A system can retrieve the right text, rank it highly, and still produce a legally wrong answer if nothing checks the date at claim time.

### Evidence

- `corpus/provisions.jsonl` — 4 `gratuity1972` provisions, each with `effective_to=2025-11-20`.
- `tests/test_corpus_retrieval.py::test_repealed_act_is_marked_and_has_date_bounds`
- `tests/test_corpus_retrieval.py::test_toolbox_currency_marks_repealed_act_out_of_force_after_commencement`
- `tests/test_corpus_retrieval.py::test_toolbox_currency_marks_repealed_act_in_force_historically`
- `evals/golden.jsonl` — `historical` expects the 1972 Act; `fixed_expiry` expects the 2020 Code.

## 2. A proviso is not an optional extra

### Problem

The decisive rule is often in the exception. For gratuity:

- s. 53(1) requires five years of continuous service;
- the **second proviso** waives that on death, disablement, or expiry of a fixed-term contract;
- s. 53(2) fixes the rate, and its **third proviso** gives pro-rata treatment to fixed-term employees.

If a generic splitter merges the proviso into the main clause, the chunk still contains the rule, but top-k retrieval can return the rule without the exception. The answer looks complete and is wrong.

### Resolution

The corpus stores each proviso as its own provision, and the chunker operates on one provision at a time. Every chunk repeats a statute/section/status header, so a standalone proviso can be ranked and cited without the parent chunk. It also records `chunking_config` in the manifest so the chunking regime is auditable.

The verification layer separately requires the fact-pattern qualifier to appear in the draft's operative citations. Missing the second proviso is a failed run even when s. 53(1) is quoted correctly.

### Evidence

- `tests/test_corpus_retrieval.py::test_proviso_is_a_separate_provision`
- `tests/test_corpus_retrieval.py::test_chunker_repeats_the_header_and_keeps_parts`
- `tests/test_verification.py` — qualifier failure cases
- Golden-set `fixed_expiry`: v1 recall 67% / failed; v2 recall 100% / passed.

## 3. The embedding hash ignored its dimension

### Problem

The deterministic hashing embedder originally hashed a feature without passing the configured embedding dimension into the hash. A non-default `EMBEDDING_DIM` could therefore produce a bucket that did not belong to the vector space the embedder was actually filling. This is the kind of bug that passes a single default-dimension test and fails as soon as someone configures a different dimension.

### Resolution

`_stable_hash(feature, dim)` now takes the dimension explicitly, and the embedder's `fingerprint` is `hashing:v1:<dim>`. The SQLite cache key is `sha256(fingerprint + "|" + text)`, so vectors from a different dimension can never be served for a new build. Index loading also rejects `vectors.shape[0] != len(chunks)`.

### Evidence

- `src/compliance/corpus/embedder.py`
- `tests/test_corpus_retrieval.py::test_hashing_embedder_is_deterministic_and_normalised` (64 dimensions)
- `tests/test_corpus_retrieval.py::test_embedding_cache_reuses_unchanged_text` (32 dimensions)
- The production index uses 384 dimensions and loads with `float32`.

## 4. Queue depth was measuring the wrong thing

### Problem

The first queue-depth gauge used `XLEN`. Redis Streams do not delete acknowledged entries; they remain until `MAXLEN` trimming. After the first burst, `XLEN` stayed non-zero forever, so the backlog alert would fire on an empty queue. The load test made this visible: the run had drained, `XPENDING` was zero, but `XLEN` was still large.

### Resolution

`RedisStreamQueue.depth()` now returns:

```text
lag + pending
```

where `lag` is the consumer-group lag (entries never delivered) and `pending` is delivered-but-unacknowledged. If the Redis client cannot provide `lag`, it falls back to `XLEN - pending`; it never reports raw `XLEN` as backlog.

### Evidence

- `src/compliance/service/queue.py`
- `tests/test_service.py::test_redis_queue_depth_uses_lag_plus_pending_not_retained_length`
- `tests/test_service.py::test_redis_queue_depth_falls_back_when_lag_is_unknown`
- After the load test: `XLEN=322`, `lag=0`, `pending=0`, `compliance_queue_depth=0`, DLQ `0`.

## 5. Docker built the load-test stage by default

### Problem

The Dockerfile ends with a `loadtest` stage so Locust can be installed without shipping it in the runtime image. Docker's default target is the **last stage**. `docker compose build api` therefore produced an image containing Locust and the test tooling, even though the `api` service was supposed to be the slim runtime.

### Resolution

Every application service pins the target:

```yaml
build:
  context: .
  dockerfile: Dockerfile
  target: runtime
```

The `locust` service explicitly pins `target: loadtest`. The runtime image was inspected after rebuild; it contains no `locust` or `pytest`.

### Evidence

- `docker-compose.yml`
- `Dockerfile`
- `docker compose build api` followed by an import check inside the image.

## 6. SELinux denied the monitoring bind mounts

### Problem

On this Fedora host, Prometheus and Grafana failed to read bind-mounted configuration. The containers exited with permission errors even though the files were world-readable on the host. This is SELinux's container label enforcement, not ordinary Unix permissions.

### Resolution

The Compose mounts carry an SELinux relabel suffix:

```yaml
- ./monitoring/prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro,z
- ./monitoring/prometheus/alerts.yml:/etc/prometheus/alerts.yml:ro,z
- ./monitoring/grafana/provisioning:/etc/grafana/provisioning:ro,z
- ./monitoring/grafana/dashboards:/var/lib/grafana/dashboards:ro,z
```

The load-test report mount uses `:z` because the container writes CSV files back to the host.

### Evidence

- `docker-compose.yml`
- `docker compose ps` — both monitoring services running after the labels were added.

## 7. Shutdown called the wrong close method

### Problem

The API lifespan closes resources on shutdown. One LLM client exposes `aclose()` (async) and not `close()`. The shutdown path assumed `close`, so shutdown raised an `AttributeError` and left the process with a noisy, avoidable failure. A test suite that only exercised request handling would not catch it.

### Resolution

`_close_resource` prefers `aclose`, falls back to `close` or `stop`, and logs rather than raising if a resource cannot be closed. A regression test constructs a client with only `aclose` and exercises shutdown.

### Evidence

- `src/compliance/api.py::_close_resource`
- `tests/test_api.py::test_shutdown_closes_llm_clients_that_use_aclose`

## 8. The load test could accidentally test the cache and a single rate-limit bucket

### Problem

Two easy ways to produce a meaningless load test:

1. Send the same request every time, so the whole-run cache serves most of the load and the worker is never exercised.
2. Use one `X-Client-Id`, so all users share one token bucket and the test measures the rate limiter rather than the advisory path.

### Resolution

`loadtest/locustfile.py` adds a unique `notes` value per request (the cache key includes it) and a unique client id per simulated user. It also records a synthetic `ADVISORY / e2e` row per run, so the reported latency is submit-to-terminal, not the admission call. A verification failure on a completed run is surfaced as a failed request.

### Evidence

- `loadtest/locustfile.py`
- `reports/loadtest/compose_failures.csv` — header only, 0 failures
- Load report — 3.06 runs/s with the cache deliberately bypassed.

## 9. Console and CSV counts differed by one

### Problem

The final Locust console summary reported 185 advisory POSTs; the checked-in CSV reported 184. The end-to-end count was 181 in both. The difference is consistent with a submission at the teardown boundary being included in the live console aggregate but not in the final CSV snapshot. This is a reporting artifact, not a lost or failed request.

### Resolution

The report treats the CSV as authoritative and states the discrepancy explicitly. It does not silently round the two numbers into agreement. The SLO conclusion is unaffected: the e2e sample is 181 runs, 0 failures, p95 930 ms.

### Evidence

- `reports/loadtest/compose_stats.csv`
- `reports/load_report.md` — “Caveats and anomalies”

## 10. A code-only policy change was hidden by the cache

### Problem

The whole-run cache key included the request, variant, index version, and prompt hashes. Those are the obvious inputs, but they are not the only ones: the deterministic composer and verifier can change without any prompt or corpus change. During final smoke testing, a rebuilt image still served the **previous** advisory for the same synthetic question because the normalized request, index, and prompts were unchanged. The output also showed the old calculation, which is how the issue was noticed.

### Resolution

`cache_key` now includes a 16-hex fingerprint of the deterministic policy modules:

- `agents/graph.py`
- `agents/nodes.py`
- `agents/policy.py`
- `agents/state.py`
- `agents/verification.py`

The fingerprint is computed once per process from the module source (`policy_fingerprint`). A code-only change to the composer, verifier, or graph therefore invalidates old cache entries just as a prompt or index change does.

### Evidence

- `src/compliance/service/cache.py`
- `tests/test_service.py::test_cache_key_covers_every_input_that_changes_the_answer`
- After the fix and a rebuild, the same smoke request returns the new advisory with no arithmetic for the below-threshold case.

### Lesson

Cache keys should include the **version of the code that computes the value**, not only the inputs it reads. This is the same class of bug as serving a stale Docker layer or a stale index: the artifact changed, but the key did not.

## 11. A “not payable” answer still showed a gratuity calculation

### Problem

The deterministic composer correctly said that 36 months of service is below the five-year threshold, but it then added the s. 53(2) rate, an indicative ₹77,885 calculation, and the s. 53(6) forfeiture rule. The prose was legally correct and verification passed, but the arithmetic implied an entitlement that did not exist. A reader skimming the calculation could reasonably reach the wrong conclusion.

### Resolution

The rate, arithmetic, and forfeiture claims are now added only when the five-year threshold is met or waived. The below-threshold branch still cites s. 53(1) and the coverage threshold, and the summary says “not payable”. The historical 1972 Act path applies the same rule.

### Evidence

- `src/compliance/agents/policy.py::_compose_gratuity_current`
- `src/compliance/agents/policy.py::_compose_gratuity_historical`
- `tests/test_golden_matrix.py::test_below_threshold_case_does_not_show_a_gratuity_calculation`

### Lesson

Verification checks whether claims are supported, not whether every supported claim is **relevant**. A calculation can be grounded in a real provision and still be misleading when the entitlement condition fails. Policy composition has to encode that relevance, not leave it to the reader.

## What was not “fixed”

Several things are deliberate design choices rather than unresolved bugs:

- **The corpus is small.** 46 provisions and 82 chunks. It is a focused legal desk, not a general legal search engine.
- **The embedder is lexical.** The hashing embedder cannot resolve synonyms the way a sentence-transformer can; the optional MiniLM provider is the upgrade path.
- **The provider is a stub.** It exercises the pipeline deterministically and makes load results reproducible, but it says nothing about frontier-model quality.
- **The queue is at-least-once.** The worker acknowledges only after the result is stored; a crash can redeliver a run. Run IDs and the store make that observable; it is not exactly-once.
- **Quarantine is not a PII scrubber.** `consent=true` is an attestation by the caller; the system does not attempt to detect real personal data.
- **There is no authentication.** The demo API is intended to sit behind a gateway in any real deployment.

## Final verification state

| Check | Result |
|---|---|
| `PYTHONPATH=src python3 -m pytest -q` | **135 passed** |
| `python3 -m ruff check src tests evals loadtest services scripts` | clean |
| `PYTHONPATH=src python3 evals/run_evals.py --variant both` | v2 100% success on 24 cases, 4 correct escalations |
| Compose stack | API/worker/Redis/stub/Jaeger healthy, Prometheus/Grafana up |
| Load test, baseline | 181 e2e runs, 0 failures, p95 930 ms, 3.06 runs/s |
| Load test, 120 users / 1 worker | 126 e2e runs, p95 22.0 s, 4.34 runs/s — SLO breach |
| Load test, 120 users / 3 workers | 386 e2e runs, p95 8.7 s, 13.27 runs/s — SLO pass |
| Cache | p95 895.3 ms miss → 19.9 ms hit; incremental cost $0 |
| Queue after drain | backlog 0, pending 0, DLQ 0 |
| Runtime image | no `pytest`/`locust` |
| Demonstration evidence | all ten checks reproduced; see [`reports/demo_evidence.md`](demo_evidence.md) |
| Incident log | 7 genuine entries, including the unverified-draft leak |
