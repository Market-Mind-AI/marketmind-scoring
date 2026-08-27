"""Chooses the product's reference-market CANDIDATES — a ranked shortlist of the
supported countries most likely to be its biggest markets. This is a real-market-
knowledge task (which countries actually buy the most), which the local model
fails at (it omitted Brazil for air fryer), so it is resolved for the dedicated
`reference_market` site and defaults to a REAL cloud LLM (see resolver.py). The
LLM only PROPOSES candidates with their localized terms; the analysis then
collects each candidate's real Google-search volume and the largest becomes the
Demand Volume ruler ceiling (Cap. 4 §4.2.1). So the metric stays deterministic
(the candidate set is query-independent) and grounded in real data — the LLM
can't set a score by itself.

Best-effort: a failure returns an empty shortlist (logged), and the analysis
falls back to the fixed ruler bound. The prompt lives HERE (provider-agnostic)."""

from pydantic import BaseModel, Field

from app.core.exceptions import MarketMindCoreError
from app.core.logging import logger
from app.services.llm import LLMError, resolve_reference_market_llm
from app.services.vision_client import MarketTerms


class ReferenceMarketError(MarketMindCoreError):
    """The reference-market LLM call failed or returned something unusable."""


class ReferenceCandidatesResult(BaseModel):
    """Structured output: up to ~8 candidate biggest-markets, ranked."""

    candidates: list[MarketTerms] = Field(default_factory=list)


_SYSTEM = (
    "You are a market-research expert. Given an already-identified product, you name the countries with the "
    "LARGEST real consumer demand for it worldwide — its biggest markets — using your knowledge of actual "
    "market size and adoption. You do NOT identify the product; you rank markets. For each market you also "
    "give the real search terms a local buyer types (the original English/Latin-script names plus "
    "local-language colloquial names — short keyword-style queries, never long descriptive phrases). When a "
    "BRAND was given, EVERY term must name that brand (never the bare generic name, never another brand); "
    "with no brand, the product is the category and no term may carry a brand. The volumes of these terms "
    "get SUMMED into one product's demand, so a term for the wrong subject corrupts the measurement. You only "
    "PROPOSE candidates; their real online search volume will decide the single biggest. Return ONLY the "
    "structured result."
)


class ReferenceMarketClient:
    def __init__(self) -> None:
        # Provider + model resolved from .env for the `reference_market` site
        # (default: a cloud LLM). Fails fast if misconfigured.
        self._provider = resolve_reference_market_llm()

    async def candidates(
        self, *, product_name: str, brand: str | None, category: str | None, supported_countries: list[str]
    ) -> list[MarketTerms]:
        """Return up to ~8 ranked candidate biggest-markets (from the supported
        basket), each with its localized terms. Raises ReferenceMarketError on
        provider failure."""
        if not supported_countries:
            return []
        product_line = f"{brand} {product_name}".strip() if brand else product_name
        prompt = (
            f"Product: {product_line}\n"
            f"Generic name: {product_name}\n"
            f"Brand: {brand or '(none)'}\n"
            f"Category: {category or '(unknown)'}\n\n"
            "From the SUPPORTED countries below, return the 8 with the HIGHEST real consumer demand for THIS "
            "product worldwide, RANKED biggest-first, each as a `candidates` entry: `country` = ISO3; "
            "`primary_name` = the single most-used real search term (local language); `search_terms` = the "
            "real high-volume queries a local buyer types (brand+model + variants + the original English/Latin "
            "names + local-language names), keyword-style, never long descriptive phrases; `excluded_terms` = "
            "look-alike terms meaning something else; `confidence` = 0-1. Include every plausibly-large market "
            f"— real search volume decides the winner. Supported countries: {', '.join(supported_countries)}."
        )
        try:
            result = await self._provider.complete(
                system=_SYSTEM, user=prompt, response_model=ReferenceCandidatesResult
            )
        except LLMError as exc:
            raise ReferenceMarketError(f"Reference-market LLM call failed: {exc}") from exc
        if not isinstance(result, ReferenceCandidatesResult):  # defensive; schema is provider-enforced
            raise ReferenceMarketError("Reference-market LLM returned an unexpected result type")
        logger.info(f"Reference-market candidates: {[c.country for c in result.candidates]}")
        return result.candidates
