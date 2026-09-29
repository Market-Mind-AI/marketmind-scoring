"""Resolves the LLM provider for each call site from the env — the service
locator. Swap provider/model by editing .env only (see config.py):
``<site>_llm_provider`` (claude|gpt|ollama) + ``<site>_llm_model``. Every site
is independent."""

from app import observability
from app.core.config import settings
from app.services.llm.base import LLMError, LLMProvider
from app.services.llm.providers import ClaudeProvider, GptProvider, OllamaProvider
from app.services.llm.traced import TracedProvider


def resolve_llm(*, provider: str, model: str, site: str | None = None) -> LLMProvider:
    """``site`` names the call site in the trace; without it, or without
    tracing configured, the bare provider comes back."""
    resolved = _build(provider, model)
    resolved.site = site
    if site and observability.tracing_enabled():
        return TracedProvider(resolved, site)
    return resolved


def _build(provider: str, model: str) -> LLMProvider:
    code = provider.strip().lower()
    if code == "claude":
        return ClaudeProvider(
            model,
            settings.anthropic_api_key,
            settings.anthropic_workspace_id,
            cache_ttl=settings.llm_prompt_cache_ttl,
        )
    if code == "gpt":
        return GptProvider(model, settings.openai_api_key)
    if code == "ollama":
        return OllamaProvider(model, settings.ollama_llm_api_base_url, settings.ollama_llm_timeout_seconds)
    raise LLMError(f"Unknown LLM provider '{provider}' (expected one of: claude, gpt, ollama)")


def resolve_vision_llm() -> LLMProvider:
    """The identify-image call (a vision model)."""
    return resolve_llm(provider=settings.vision_llm_provider, model=settings.vision_llm_model, site="vision")


def resolve_enrichment_llm() -> LLMProvider:
    """Fills the JSON fields vision left out — above all localized_markets
    (a TEXT model, since the local vision model is unreliable at producing
    localized search terms)."""
    return resolve_llm(
        provider=settings.enrichment_llm_provider, model=settings.enrichment_llm_model, site="enrichment"
    )


def resolve_reference_market_llm() -> LLMProvider:
    """Chooses the reference-market candidates (the product's biggest markets) —
    a real-market-knowledge task, so it defaults to a cloud LLM."""
    return resolve_llm(
        provider=settings.reference_market_llm_provider,
        model=settings.reference_market_llm_model,
        site="reference_market",
    )


def resolve_explanation_llm() -> LLMProvider:
    """The final MOS-narrative call (text)."""
    return resolve_llm(
        provider=settings.explanation_llm_provider, model=settings.explanation_llm_model, site="explanation"
    )


def resolve_catalog_match_llm() -> LLMProvider:
    """Decide se o texto consultado é o mesmo produto de alguma entidade do
    catálogo, quando alias exato e trigram falham (ver catalog_matcher.py).
    Substituiu o passo de embedding: os casos que sobram são sinônimo de
    domínio e idiomas sem parentesco de escrita, que distância vetorial não
    resolve."""
    return resolve_llm(
        provider=settings.catalog_match_llm_provider, model=settings.catalog_match_llm_model, site="catalog_match"
    )
