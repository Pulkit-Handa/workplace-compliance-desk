"""Failure-path tests for the provider circuit breaker and fallback.

These are the cheap, deterministic proof of the degradation contract: a
provider that keeps failing opens the breaker, an open breaker refuses new
calls, and the fallback path is marked degraded rather than silently presented
as a normal answer.
"""
from __future__ import annotations

import pytest

from compliance.llm.base import LLMResult
from compliance.llm.circuit import CircuitBreaker, CircuitOpenError, CircuitState
from compliance.llm.http import FallbackLLMClient


def test_circuit_opens_rejects_and_recovers_after_a_probe():
    breaker = CircuitBreaker(threshold=2, cooldown_s=0.0)
    breaker.record_failure("provider timeout")
    assert breaker.state is CircuitState.CLOSED
    breaker.record_failure("provider timeout again")
    assert breaker.state is CircuitState.OPEN

    # A zero cooldown allows exactly one half-open probe.
    breaker.before_call()
    assert breaker.state is CircuitState.HALF_OPEN
    with pytest.raises(CircuitOpenError, match="half-open"):
        breaker.before_call()
    breaker.record_success()
    assert breaker.state is CircuitState.CLOSED
    assert breaker.failures == 0
    assert breaker.snapshot()["transitions"] >= 3


class _FailingProvider:
    provider = "primary"
    circuit_snapshot = lambda self: {"state": "open"}  # noqa: E731

    async def complete(self, **_kwargs):
        raise RuntimeError("provider unavailable")

    async def aclose(self) -> None:
        pass


class _LocalFallback:
    provider = "local"

    async def complete(self, **_kwargs):
        return LLMResult(
            text='{"ok": true}',
            provider="local",
            model="deterministic",
            prompt_tokens=1,
            completion_tokens=1,
            cost_usd=0.0,
        )

    async def aclose(self) -> None:
        pass


@pytest.mark.asyncio
async def test_fallback_marks_the_run_degraded_instead_of_hiding_the_failure():
    wrapper = FallbackLLMClient(_FailingProvider(), _LocalFallback())
    result = await wrapper.complete(task="draft", prompt="x", prompt_version="draft-v2")
    assert result.fallback_used is True
    assert "provider unavailable" in (result.error or "")
    assert wrapper.circuit_snapshot()["state"] == "open"
