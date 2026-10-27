# Token and cost accounting

## Scope

This report records the token and cost metadata emitted by the production Compose path
(`stub_http` provider, Redis Streams, one worker). The provider is deterministic and has
no invoice, so the stack is configured with explicit **shadow prices**:

```text
LLM_INPUT_COST_PER_MTOK=0.15
LLM_OUTPUT_COST_PER_MTOK=0.60
```

The shadow price exists to exercise the accounting code and the dashboards. It is **not**
a claim about a paid model's invoice. At the stub provider's real price (free), every
figure below would be $0.00.

The measured sample is 10 fresh advisory runs with the same question shape and distinct
synthetic fact patterns. The raw records, including every `usage.per_step` entry, are in
[`reports/cache_study.json`](cache_study.json). The run totals are also exposed in the API
result and in Prometheus (`compliance_llm_tokens_total`,
`compliance_llm_cost_usd_total`, `compliance_run_cost_usd`).

## Per-step accounting

Measured over 10 fresh runs:

| Step | Prompt tokens / run | Completion tokens / run | Cost / run (USD) | Share of run cost | Mean provider latency |
|---|---:|---:|---:|---:|---:|
| `intake` | 270.0 | 1.0 | 0.00004110 | 4.8% | 159.9 ms |
| `draft` | 2,525.8 | 1.0 | 0.00037947 | 43.9% | 194.3 ms |
| `verify` | 2,655.8 | 1.0 | 0.00039897 | **46.1%** | 194.5 ms |
| `route` | 298.0 | 1.0 | 0.00004530 | 5.2% | 194.1 ms |
| **Total** | **5,749.6** | **4.0** | **0.00086484** | **100%** | — |

`usage.per_step` stores prompt tokens, completion tokens, cost, latency, provider, model,
and prompt version for every model call. The per-run `usage` object sums those fields.
The same values feed the Prometheus token and cost counters.

## Per-run and per-100-run cost

| Measurement | Value |
|---|---:|
| Prompt tokens / run | 5,749.6 |
| Completion tokens / run | 4.0 |
| Total tokens / run | 5,753.6 |
| LLM calls / run | 4 |
| Cost / run | **$0.00086484** |
| Cost / 100 fresh runs | **$0.086484** |
| Cost / 100 runs with 1 miss + 99 cache hits | **$0.000865** |

The **largest cost driver is the `verify` step at 46.1% of run cost** (2,655.8 prompt
tokens per run). `draft` is a close second at 43.9%. Input tokens dominate because the
stub completes with `{}`; with a real model, completion tokens would move the ranking.
The caching result is therefore important: repeated questions avoid four provider calls
and the entire per-step cost.

## Cache effect

Ten repeated pairs, each pair using the same request (the first call is a miss and the
second is a whole-run cache hit):

| Measurement | Miss | Hit | Change |
|---|---:|---:|---:|
| Samples | 10 | 10 | — |
| p50 wall time | 820.5 ms | 17.3 ms | -97.9% |
| p95 wall time | **895.3 ms** | **19.9 ms** | **-97.8%** |
| Provider calls per run | 4 | 0 incremental | -100% |
| Incremental cost per run | $0.00086484 | **$0.00** | -100% |
| Cost for the 10-sample set | $0.0086484 | **$0.00** | -100% |

A cache hit returns the original result for auditability, including the original
`usage`/`cost_usd` metadata. That metadata describes the **logical** run that produced
the answer, not a new provider charge. The incremental cost of a hit is zero because the
API short-circuits before enqueueing and no model call is made. Dashboards should
therefore read cost from the miss path and use `compliance_runs_cached_total` to reason
about avoided cost, not sum the echoed metadata.

## Reproduction

```bash
# The shadow prices are in docker-compose.yml.
docker compose up -d --build

# Run 10 fresh + 10 repeated requests against the Compose API.
python3 scripts/cache_study.py
```

The study script is intentionally small and uses synthetic data only. A fresh run
rewrites `reports/cache_study.json`; the checked-in figures above are from the
2026-10-26 measurement.

## Caveats

- **Stub provider.** The 150 ms injected delay and `{}` completion are synthetic. Real
  models have different tokenisation, completion lengths, and price schedules.
- **Shadow price.** The `$0.15/$0.60` rates are accounting assumptions, not a quote.
  Update the two environment variables when a real provider is wired in.
- **One question shape.** The per-step token mix depends on the prompt variant and the
  retrieved context. This is a representative gratuity path, not every domain.
- **Cached metadata.** Cached results echo the original usage. They are not charged
  again, and the API does not emit a second cost observation for the hit.
