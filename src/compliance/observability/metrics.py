from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# ------------------------------------------------------------------ runs
RUNS_TOTAL = Counter(
    "compliance_runs_total",
    "Completed advisory runs",
    ["status", "route", "domain"],
)
RUN_DURATION = Histogram(
    "compliance_run_duration_seconds",
    "End-to-end run latency, submission to final result",
    buckets=(0.25, 0.5, 1, 2, 3, 5, 8, 12, 20, 30, 45, 60, 120),
)
RUN_COST = Histogram(
    "compliance_run_cost_usd",
    "Model cost per advisory run",
    buckets=(0, 0.0001, 0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1),
)
RUNS_CACHED_TOTAL = Counter("compliance_runs_cached_total", "Runs served from whole-run cache")
RUNS_DEGRADED_TOTAL = Counter("compliance_runs_degraded_total", "Runs that took a degraded path")
ESCALATIONS_TOTAL = Counter(
    "compliance_escalations_total", "Escalations by reason", ["reason", "route"]
)
QUARANTINE_TOTAL = Counter(
    "compliance_quarantine_total", "Requests rejected at ingestion and quarantined", ["reason"]
)
RATE_LIMITED_TOTAL = Counter(
    "compliance_rate_limited_total", "Requests rejected by the per-client rate limiter", ["client"]
)

# ------------------------------------------------------------------ agents / tools
AGENT_DURATION = Histogram(
    "compliance_agent_duration_seconds",
    "Latency per agent node",
    ["agent"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 12, 20),
)
AGENT_SUCCESS = Counter(
    "compliance_agent_success_total", "Agent node outcomes", ["agent", "outcome"]
)
TOOL_CALLS = Counter(
    "compliance_tool_calls_total", "Tool invocations", ["tool", "outcome"]
)
TOOL_DURATION = Histogram(
    "compliance_tool_duration_seconds",
    "Latency per tool",
    ["tool"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
)
STEPS_PER_RUN = Histogram(
    "compliance_steps_per_run",
    "Agent steps per run",
    buckets=(1, 2, 3, 4, 5, 6, 8, 10, 12, 15),
)
TOOL_CALLS_PER_RUN = Histogram(
    "compliance_tool_calls_per_run",
    "Tool calls per run",
    buckets=(0, 1, 2, 3, 4, 5, 6, 8, 10, 12),
)
RETRIES_TOTAL = Counter(
    "compliance_retries_total",
    "Research retries triggered by a verification failure",
    ["reason"],
)
# Publish a zero-valued child so the dashboard shows "0 retries", not "No data".
RETRIES_TOTAL.labels(reason="verification_failure").inc(0)

# ------------------------------------------------------------------ verification
VERIFICATION_TOTAL = Counter(
    "compliance_verification_total",
    "Verification outcomes",
    ["outcome"],
)
VERIFICATION_FAILURES = Counter(
    "compliance_verification_failures_total",
    "Verification failure reasons",
    ["reason"],
)

# ------------------------------------------------------------------ llm / provider
LLM_CALLS = Counter(
    "compliance_llm_calls_total", "Model calls", ["provider", "task", "outcome"]
)
LLM_TOKENS = Counter(
    "compliance_llm_tokens_total", "Prompt and completion tokens", ["kind"]
)
LLM_COST = Counter("compliance_llm_cost_usd_total", "Cumulative model cost in USD")
LLM_LATENCY = Histogram(
    "compliance_llm_latency_seconds",
    "Model call latency",
    ["provider", "task"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 15),
)
PROVIDER_FAILURES = Counter(
    "compliance_provider_failures_total", "Provider call failures", ["reason"]
)
CIRCUIT_STATE = Gauge(
    "compliance_circuit_breaker_state",
    "Circuit breaker state, 1 for the active state",
    ["state"],
)
CIRCUIT_TRANSITIONS = Counter(
    "compliance_circuit_breaker_transitions_total",
    "Circuit breaker transitions",
    ["to_state"],
)

# ------------------------------------------------------------------ retrieval / cache
RETRIEVAL_LATENCY = Histogram(
    "compliance_retrieval_latency_seconds",
    "Retrieval latency",
    ["kind"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2),
)
CACHE_OPS = Counter(
    "compliance_cache_ops_total", "Cache hits and misses", ["kind", "result"]
)
PASSAGES_RETURNED = Histogram(
    "compliance_passages_returned",
    "Passages returned per search",
    buckets=(0, 1, 2, 3, 4, 5, 6, 8, 10, 15, 20),
)

# ------------------------------------------------------------------ index freshness
INDEX_CHUNKS = Gauge("compliance_index_chunks", "Number of chunks in the active index")
INDEX_BUILD_TIMESTAMP = Gauge(
    "compliance_index_build_timestamp_seconds", "Unix timestamp of the active index build"
)
INDEX_BUILD_DURATION = Histogram(
    "compliance_index_build_duration_seconds",
    "Duration of index builds",
    buckets=(0.1, 0.5, 1, 2, 5, 10, 20, 30, 60, 120),
)
INDEX_EMBEDDED_CHUNKS = Counter(
    "compliance_index_embedded_chunks_total", "Chunks embedded, not reused from cache"
)
INDEX_REUSED_CHUNKS = Counter(
    "compliance_index_reused_chunks_total", "Chunks whose vectors were reused"
)

# ------------------------------------------------------------------ queue / service
QUEUE_DEPTH = Gauge("compliance_queue_depth", "Unprocessed entries in the run stream (lag + pending)")
QUEUE_PENDING = Gauge(
    "compliance_queue_pending_entries", "Delivered-but-unacked entries in the consumer group"
)
RUNS_QUEUED = Counter("compliance_runs_queued_total", "Runs accepted and queued")
ACTIVE_RUNS = Gauge("compliance_active_runs", "Runs currently executing in this worker")

# ------------------------------------------------------------------ quality / feedback
FEEDBACK_TOTAL = Counter(
    "compliance_feedback_total", "Caller feedback", ["satisfactory", "category"]
)
QUALITY_GROUNDEDNESS = Gauge(
    "compliance_quality_groundedness", "Sampled live-run groundedness", ["source"]
)
QUALITY_CONTEXT_RELEVANCE = Gauge(
    "compliance_quality_context_relevance", "Sampled live-run context relevance", ["source"]
)
QUALITY_FAITHFULNESS = Gauge(
    "compliance_quality_faithfulness", "Sampled live-run faithfulness", ["source"]
)
QUALITY_HALLUCINATION_RATE = Gauge(
    "compliance_quality_hallucination_rate", "Sampled live-run hallucination rate", ["source"]
)
