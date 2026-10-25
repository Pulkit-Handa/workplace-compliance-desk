from __future__ import annotations

from compliance.config import settings
from compliance.llm.base import LLMClient
from compliance.llm.http import FallbackLLMClient, HttpLLMClient
from compliance.llm.local import LocalDeterministicLLM

_client: LLMClient | None = None


def make_llm_client() -> LLMClient:
    if settings.llm_provider == "stub_local":
        return LocalDeterministicLLM()
    if settings.llm_provider == "stub_http":
        primary = HttpLLMClient(
            provider="stub_http",
            base_url=settings.llm_base_url,
            model=settings.llm_model,
        )
        return FallbackLLMClient(primary, LocalDeterministicLLM())
    if settings.llm_provider == "openai_compatible":
        if not settings.llm_api_key:
            raise ValueError("LLM_API_KEY is required for openai_compatible")
        primary = HttpLLMClient(
            provider="openai_compatible",
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
        )
        return FallbackLLMClient(primary, LocalDeterministicLLM())
    raise ValueError(f"unsupported LLM_PROVIDER {settings.llm_provider!r}")


def get_llm_client() -> LLMClient:
    global _client
    if _client is None:
        _client = make_llm_client()
    return _client


def reset_llm_client() -> None:
    global _client
    _client = None
