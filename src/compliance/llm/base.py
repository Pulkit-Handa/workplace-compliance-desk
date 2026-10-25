from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    fallback_used: bool = False
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LLMClient(Protocol):
    provider: str

    async def complete(
        self,
        *,
        task: str,
        prompt: str,
        prompt_version: str,
        temperature: float = 0.0,
        max_tokens: int = 700,
        json_mode: bool = True,
    ) -> LLMResult: ...

    async def aclose(self) -> None: ...

    def circuit_snapshot(self) -> dict: ...


class LLMError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable
