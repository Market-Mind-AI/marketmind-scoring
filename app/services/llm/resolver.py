"""Resolves the LLM provider for each call site from the env — the service
locator. Swap provider/model by editing .env only (see config.py):
``<site>_llm_provider`` (claude|gpt|ollama) + ``<site>_llm_model``. Every site
is independent."""

from app.core.config import settings
from app.services.llm.base import LLMError, LLMProvider
from app.services.llm.providers import ClaudeProvider, GptProvider, OllamaProvider


def resolve_llm(*, provider: str, model: str) -> LLMProvider:
    code = provider.strip().lower()
    if code == "claude":
        return ClaudeProvider(model, settings.anthropic_api_key)
    if code == "gpt":
        return GptProvider(model, settings.openai_api_key)
    if code == "ollama":
        return OllamaProvider(model, settings.ollama_llm_api_base_url, settings.ollama_llm_timeout_seconds)
    raise LLMError(f"Unknown LLM provider '{provider}' (expected one of: claude, gpt, ollama)")


def resolve_vision_llm() -> LLMProvider:
    """The identify-image call (a vision model)."""
    return resolve_llm(provider=settings.vision_llm_provider, model=settings.vision_llm_model)


def resolve_enrichment_llm() -> LLMProvider:
    """Fills the JSON fields vision left out — above all localized_markets
    (a TEXT model, since the local vision model is unreliable at producing
    localized search terms)."""
    return resolve_llm(provider=settings.enrichment_llm_provider, model=settings.enrichment_llm_model)


def resolve_explanation_llm() -> LLMProvider:
    """The final MOS-narrative call (text)."""
    return resolve_llm(provider=settings.explanation_llm_provider, model=settings.explanation_llm_model)
