from __future__ import annotations

import logging
from typing import Protocol

from src.http import HTTPClient
from src.settings import Settings

logger = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    pass


class AIProvider(Protocol):
    name: str

    async def generate(self, system: str, prompt: str, *, max_tokens: int = 900) -> str: ...


class FallbackProvider:
    name = "gemini+groq"

    def __init__(self, providers: list[AIProvider]):
        self.providers = providers
        self.last_provider: str | None = None

    async def generate(self, system: str, prompt: str, *, max_tokens: int = 900) -> str:
        self.last_provider = None
        for provider in self.providers:
            try:
                text = await provider.generate(system, prompt, max_tokens=max_tokens)
                if not text.strip():
                    raise ProviderError("empty AI response")
                self.last_provider = provider.name
                return text
            except Exception as exc:
                logger.warning("AI provider %s unavailable (%s)", provider.name, type(exc).__name__)
        raise ProviderError("No configured AI provider succeeded")


def build_provider(settings: Settings, http: HTTPClient, budget=None, *, store=None) -> FallbackProvider:
    from src.ai.gemini import GeminiProvider
    from src.ai.groq import GroqProvider

    providers: list[AIProvider] = []
    if settings.gemini_api_key:
        providers.append(GeminiProvider(http, settings.gemini_api_key, settings.gemini_model, budget))
    if settings.groq_api_key:
        providers.append(GroqProvider(http, settings.groq_api_key, settings.groq_model, budget))
    if store is not None:
        from src.diagnostics import ObservedProvider
        def reserve(provider):
            store.reserve_ai_request(settings.ai_requests_per_day, provider.name)
            provider.request_count += 1
        for provider in providers:
            provider.request_count = 0
            provider.budget = lambda provider=provider: reserve(provider)
        providers = [ObservedProvider(provider, store) for provider in providers]
    return FallbackProvider(providers)
