"""Demonstrate a real provider connection failure opening the circuit breaker.

The primary provider points at an intentionally closed local port.  One failed
call opens a threshold-one breaker; the fallback returns a degraded result, and
a direct second primary call is rejected without touching the network.

Run from the repository root:

    PYTHONPATH=src python3 scripts/demo_circuit_breaker.py
"""
from __future__ import annotations

import asyncio
import json

from compliance.llm.base import LLMError
from compliance.llm.circuit import CircuitBreaker
from compliance.llm.http import FallbackLLMClient, HttpLLMClient
from compliance.llm.local import LocalDeterministicLLM


async def main() -> None:
    breaker = CircuitBreaker(threshold=1, cooldown_s=30.0)
    primary = HttpLLMClient(
        provider="demo_unavailable",
        base_url="http://127.0.0.1:9",
        timeout_s=0.5,
        max_retries=0,
        circuit=breaker,
    )
    client = FallbackLLMClient(primary, LocalDeterministicLLM())
    try:
        result = await client.complete(
            task="draft",
            prompt='{"question": "demo"}',
            prompt_version="draft-v2",
        )
        try:
            await primary.complete(
                task="draft",
                prompt='{"question": "demo"}',
                prompt_version="draft-v2",
            )
            second_call = "unexpectedly reached the provider"
        except LLMError as exc:
            second_call = str(exc)
        print(
            json.dumps(
                {
                    "fallback_used": result.fallback_used,
                    "fallback_provider": result.provider,
                    "error": result.error,
                    "circuit": client.circuit_snapshot(),
                    "second_primary_call": second_call,
                },
                indent=2,
            )
        )
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
