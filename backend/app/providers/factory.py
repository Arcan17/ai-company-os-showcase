from __future__ import annotations

from ..config import settings
from .base import Provider
from .deterministic import DeterministicProvider
from .ollama import OllamaProvider


def get_provider() -> Provider:
    if settings.llm_provider == "ollama":
        return OllamaProvider(settings.ollama_url, settings.ollama_model)
    return DeterministicProvider()
