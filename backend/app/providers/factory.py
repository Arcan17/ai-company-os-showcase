from __future__ import annotations

from sqlalchemy.engine import Engine

from ..config import settings
from .base import Provider, ProviderConfigError
from .deterministic import DeterministicProvider
from .ollama import OllamaProvider


def get_provider(eng: Engine | None = None) -> Provider:
    if settings.llm_provider == "ollama":
        return OllamaProvider(settings.ollama_url, settings.ollama_model)
    if settings.llm_provider == "anthropic":
        if eng is None:
            raise ProviderConfigError("El modelo de pago necesita la base de datos para controlar el gasto.")
        from .anthropic_provider import AnthropicProvider
        from .budget import Budget, resolve_prices
        pin, pout = resolve_prices(settings.anthropic_model)
        return AnthropicProvider(settings.anthropic_api_key, settings.anthropic_model,
                                 Budget(eng, "anthropic", settings.anthropic_model, pin, pout, settings.llm_budget_day_usd, settings.llm_budget_month_usd))
    return DeterministicProvider()
