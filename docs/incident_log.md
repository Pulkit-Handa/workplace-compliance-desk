# Incident log

This is the genuine engineering incident log for the advisory desk. Times are local
(2026-10-26). No production or personal data is included; all reproductions use synthetic
requests. The longer narrative for several entries is in
[`reports/obstacle_report.md`](../reports/obstacle_report.md).

| ID | Incident | Severity | Detection | Status |
|---|---|---|---|---|
| INC-001 | Cached advisory survived a code-only legal-policy change | High | Manual smoke compared the answer with the source and current policy | Fixed |
| INC-002 | Required provision fetch used only the first chunk of a multi-chunk section | Medium | Expanded golden set (`maternity_creche_50`) failed verification | Fixed |
| INC-003 | Queue-depth gauge reported retained stream length instead of backlog | Medium | Metrics inspection after the first burst showed a permanent backlog | Fixed |
| INC-004 | Host-published worker metrics port blocked `docker compose --scale worker=3` | Medium | Scale-up attempt failed with a port-allocation error | Fixed |
| INC-005 | Default Docker build shipped the load-test image | High | Image inspection found `pytest` and `locust` in the runtime image | Fixed |
| INC-006 | Jaeger's host OTLP ports collided with another local stack | Low | Compose start failed on `4318:4318` | Fixed |
| INC-007 | A failed draft's claims and citations were still returned with an escalation | High | Final self-verification demo showed 1 claim and 1 citation on a failed run | Fixed |

## INC-001 — stale answer after a code-only policy change

**Impact:** a rebuilt image re-served an earlier advisory for the same synthetic
question. The answer still showed a gratuity calculation for a below-threshold case,
even though the composer had been changed to omit rate, arithmetic, and forfeiture claims
when the five-year threshold is unmet.

**Root cause:** the whole-run cache key included the normalized request, variant, index
version, and prompt hashes. It did not include the deterministic policy/composer source.
No prompt or corpus file had changed, so the key was identical.

**Fix:** `policy_fingerprint()` hashes the policy-relevant modules
(`agents/graph.py`, `nodes.py`, `policy.py`, `state.py`, `verification.py`) into the cache
key. A code-only legal change now invalidates old entries.

**Evidence:** `src/compliance/service/cache.py`;
`tests/test_service.py::test_cache_key_covers_every_input_that_changes_the_answer`;
the below-threshold regression test in `tests/test_golden_matrix.py`.

## INC-002 — the crèche duty was retrieved but not cited

**Impact:** the new `maternity_creche_50` golden case failed verification on `clm_08`.
The claim required both the fifty-employee crèche duty and the four-visit entitlement,
but the cited passage was `ssc2020-s67::c2`, the intimation clause. The run was withheld
and routed to review even though the facts were answerable.

**Root cause:** `_fetch_required` skipped a provision if **any** chunk of it was already
in evidence. Search had retrieved only `c2`; the operative `c1` chunk was never fetched.
The same mechanism could hide a relevant proviso in any multi-chunk provision.

**Fix:** the required-provision fetch now checks all chunks returned by
`get_provision_chunks()` and fetches the provision when any chunk is absent. The citation
selector then chooses the best-supporting chunk for the claim.

**Evidence:** `src/compliance/agents/nodes.py::_fetch_required`;
`evals/golden.jsonl` case `maternity_creche_50`;
`reports/eval_report.md` (24-case run, v2 100% success).

## INC-003 — the queue looked permanently backlogged

**Impact:** the worker dashboard showed a non-zero queue after a burst even though every
run had completed. This would have caused false `QueueBacklogGrowing` pages.

**Root cause:** `RedisStreamQueue.depth()` used `XLEN`, which counts acknowledged entries
until they are trimmed. The stream intentionally retains entries for audit, so `XLEN` can
grow forever.

**Fix:** depth is now consumer-group `lag + pending`; `XLEN - pending` is the fallback
when lag is unavailable. The load report records both the retained `XLEN` and the
backlog-based gauge to make the distinction visible.

**Evidence:** `src/compliance/service/queue.py`;
`tests/test_service.py::test_redis_queue_depth_uses_lag_plus_pending`;
`reports/load_report.md` queue-drain section.

## INC-004 — a host port made scaling impossible

**Impact:** `docker compose up --scale worker=3` failed because every worker replica tried
to publish host port `9101`. The assignment requires a working three-worker scale-out.

**Root cause:** the worker was written like a local service with a fixed host metrics
port. Replicas cannot share a host port on the same address.

**Fix:** the worker now uses `expose: ["9101"]` and no host mapping. Prometheus still
scrapes `worker:9101` on the Compose network. Local inspection uses
`docker compose exec -T worker curl -fsS http://127.0.0.1:9101/metrics`.

**Evidence:** `docker-compose.yml`; the 1-worker vs 3-worker measurements in
[`reports/load_report.md`](../reports/load_report.md) (p95 22.0 s → 8.7 s at 120 users).

## INC-005 — the runtime image shipped test tooling

**Impact:** the app image contained `pytest` and `locust`, inflating the attack surface and
contradicting the production-image claim.

**Root cause:** the Dockerfile had no explicit build target, so Compose built the final
load-test stage for `api` and `worker`.

**Fix:** app services pin `target: runtime`; the `loadtest` stage is built only for the
Locust profile. The runtime image was re-inspected after the fix.

**Evidence:** `Dockerfile`; `docker-compose.yml`; final verification in
[`reports/obstacle_report.md`](../reports/obstacle_report.md).

## INC-006 — tracing backend port collision

**Impact:** bringing up the OTLP collector failed on host port `4318` because another
local Jaeger instance already held it. The API and worker did not start in that attempt.

**Root cause:** both stacks published the same OTLP and UI ports.

**Fix:** the project's Jaeger publishes only the UI on `16687` and exposes OTLP on the
Compose network as `jaeger:4318`; the API/worker export to
`http://jaeger:4318/v1/traces`. No host OTLP port is required.

**Evidence:** `docker-compose.yml`; `GET http://localhost:16687/api/services` returns
`compliance-api`; traces are visible in the Jaeger UI at
`http://localhost:16687`.

## INC-007 — an unverified draft leaked into the escalation response

**Impact:** a deliberate bad draft was caught by verification (`number_not_supported`,
two `qualifier_missing` issues) and the route was `legal_counsel_review`, but the result
still carried the bad claim and its citation. The rendered advisory also included the
unsupported draft under **Analysis**. That violates the rule that an unverified answer
must not be returned.

**Root cause:** `route_node` changed the route on verification failure but did not clear
`ctx.claims` / `ctx.citations`. `final_result()` therefore serialized the failed draft
next to the escalation decision.

**Fix:** when verification fails, `route_node` now clears both collections before
rendering. The escalation keeps the summary, route basis, issues, and next actions; it no
longer exposes the unverified claim. The markdown renderer also omits empty
**Provisions considered** and **Analysis** sections.

**Evidence:** `src/compliance/agents/nodes.py::route_node`;
`tests/test_golden_matrix.py::test_failed_draft_is_withheld_and_escalated`;
`scripts/demo_self_verification.py` reports `published_claims: 0` and
`published_citations: 0` after two retries.

## Follow-up rules

1. A legal-policy change is a cache-key change. Run the cache invalidation test.
2. A provision is not "retrieved" until **all** of its chunks are available to the
   citation selector.
3. Queue backlog means `lag + pending`, never `XLEN`.
4. A replica may not publish a fixed host port.
5. Production images are built with an explicit target.
6. Trace backends are reached on the Compose network; host ports are only for humans.
