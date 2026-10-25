from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All environment-specific configuration.  Nothing secret has a default."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # runtime
    compliance_env: str = "development"
    log_level: str = "INFO"
    api_port: int = 8000
    worker_metrics_port: int = 9101
    service_name: str = "compliance-api"

    # storage / paths
    redis_url: str = "redis://localhost:6379/0"
    index_dir: Path = Path("data/index")
    corpus_file: Path = Path("corpus/provisions.jsonl")
    prompts_dir: Path = Path("prompts")
    prompt_variant: str = "v2"
    index_manifest: Path = Path("corpus/sources/MANIFEST.json")
    quarantine_dir: Path = Path("data/quarantine")

    # retrieval
    embedding_provider: str = "hashing"
    embedding_dim: int = 384
    embedding_model: str = ""
    chunk_max_chars: int = 1100
    chunk_overlap_chars: int = 120
    retrieval_top_k: int = 6
    retrieval_vector_weight: float = 0.62
    retrieval_lexical_weight: float = 0.38
    retrieval_cache_ttl_s: int = 900

    # agent loop limits
    max_steps: int = 12
    max_tool_calls: int = 10
    max_wall_clock_s: int = 30
    max_verify_loops: int = 2
    max_llm_calls: int = 6
    tool_timeout_s: float = 3.0

    # provider
    llm_provider: str = "stub_local"
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_s: float = 8.0
    llm_max_retries: int = 2
    llm_retry_base_s: float = 0.25
    circuit_breaker_threshold: int = 5
    circuit_breaker_cooldown_s: float = 30.0
    llm_input_cost_per_mtok: float = 0.0
    llm_output_cost_per_mtok: float = 0.0

    # guardrails
    injection_mode: str = "quarantine"
    toxicity_mode: str = "quarantine"
    enable_output_filter: bool = True
    enable_demo_endpoints: bool = True

    # reliability
    rate_limit_per_minute: int = 60
    rate_limit_burst: int = 10
    rate_limit_enabled: bool = True
    run_cache_enabled: bool = True
    auto_eval_sample_rate: float = 0.2
    queue_backend: str = "auto"  # auto | redis | inline
    queue_stream: str = "compliance:runs"
    queue_dlq_stream: str = "compliance:runs:dead"
    queue_group: str = "workers"
    queue_consumer_prefix: str = "worker"
    queue_block_ms: int = 2000
    queue_batch: int = 5
    queue_max_retries: int = 2
    queue_retry_backoff_s: float = 1.0
    worker_concurrency: int = 1
    run_ttl_s: int = 604800
    cache_ttl_s: int = 3600
    demo_ui_title: str = "Workplace Compliance Advisory Desk"

    # telemetry
    otel_exporter_otlp_endpoint: str = ""
    otel_exporter_otlp_protocol: str = "http/protobuf"
    otel_traces_sampler_arg: float = 1.0
    prometheus_multiproc_dir: str = ""

    # SLO, declared before measurement
    slo_p95_ms: int = 20000
    slo_target_rps: float = 2.0

    # load-test provider
    stub_llm_delay_ms: int = 150
    stub_llm_fail_rate: float = 0.0

    _PROMPT_VARIANTS = {"v1", "v2"}

    def model_post_init(self, __context) -> None:  # noqa: D401
        if self.prompt_variant not in self._PROMPT_VARIANTS:
            raise ValueError(f"unknown PROMPT_VARIANT {self.prompt_variant!r}")
        if self.embedding_provider not in {"hashing", "sentence_transformers"}:
            raise ValueError("EMBEDDING_PROVIDER must be hashing or sentence_transformers")
        if self.llm_provider not in {"stub_local", "stub_http", "openai_compatible"}:
            raise ValueError("LLM_PROVIDER must be stub_local, stub_http or openai_compatible")
        if not (0 < self.retrieval_vector_weight < 1) or not (0 < self.retrieval_lexical_weight < 1):
            raise ValueError("retrieval weights must lie in (0, 1)")
        if self.queue_backend not in {"auto", "redis", "inline"}:
            raise ValueError("QUEUE_BACKEND must be auto, redis or inline")
        if self.worker_concurrency < 1:
            raise ValueError("WORKER_CONCURRENCY must be at least 1")

    def ensure_dirs(self) -> None:
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
