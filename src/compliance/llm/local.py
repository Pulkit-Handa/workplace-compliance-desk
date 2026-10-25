from __future__ import annotations

import time

from compliance.config import settings
from compliance.llm.base import LLMResult
from compliance.observability import metrics


class LocalDeterministicLLM:
    """In-process fallback.

    It refuses to invent legal text.  The agents use it only as a capability
    probe; the deterministic policy composes claims from retrieved passages when
    the model returns no usable JSON.  Token counts are estimated so that cost
    accounting and dashboards work even in this mode.
    """

    provider = "stub_local"

    def __init__(self) -> None:
        self.calls = 0

    def _estimate_tokens(self, text: str) -> int:
        return max(1, int(len(text) / 4))

    async def complete(
        self,
        *,
        task: str,
        prompt: str,
        prompt_version: str,
        temperature: float = 0.0,
        max_tokens: int = 700,
        json_mode: bool = True,
    ) -> LLMResult:
        start = time.perf_counter()
        prompt_tokens = self._estimate_tokens(prompt)
        completion_tokens = 8
        cost = (
            prompt_tokens / 1_000_000 * settings.llm_input_cost_per_mtok
            + completion_tokens / 1_000_000 * settings.llm_output_cost_per_mtok
        )
        self.calls += 1
        metrics.LLM_CALLS.labels(provider=self.provider, task=task, outcome="ok").inc()
        metrics.LLM_TOKENS.labels(kind="prompt").inc(prompt_tokens)
        metrics.LLM_TOKENS.labels(kind="completion").inc(completion_tokens)
        metrics.LLM_COST.inc(cost)
        metrics.LLM_LATENCY.labels(provider=self.provider, task=task).observe(
            time.perf_counter() - start
        )
        return LLMResult(
            text="{}",
            provider=self.provider,
            model="",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=(time.perf_counter() - start) * 1000,
            cost_usd=cost,
            raw={"prompt_version": prompt_version},
        )

    async def aclose(self) -> None:
        return None

    def circuit_snapshot(self) -> dict:
        return {"state": "closed", "failures": 0, "threshold": 0, "cooldown_s": 0}
