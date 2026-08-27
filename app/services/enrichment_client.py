"""Fills the ONE field the vision model is unreliable at but that demand
collection cannot run without: localized_markets — the real per-country search
vocabulary, which must NEVER be empty. A TEXT LLM resolved for the `enrichment`
site (default ollama / qwen3:14b); the vision model routinely returns it empty
or as bad transliterations. The reference-market CANDIDATES are a separate
concern handled by ReferenceMarketClient (a real cloud LLM), since they need
real market knowledge.

The prompt lives HERE (provider-agnostic). The LLM only proposes vocabulary from
the already-identified product identity — it computes no score (Cap. 4 §4.0)."""

from pydantic import BaseModel, Field

from app.core.exceptions import MarketMindCoreError
from app.services.llm import LLMError, resolve_enrichment_llm
from app.services.vision_client import MarketTerms


class EnrichmentError(MarketMindCoreError):
    """The enrichment LLM call failed or returned something unusable."""


class LocalizedMarketsResult(BaseModel):
    """Structured output: one MarketTerms per requested country."""

    markets: list[MarketTerms] = Field(default_factory=list)


_SYSTEM = (
    "You build the real online-search vocabulary a local buyer uses for a given product, per country, "
    "for a cross-country market-demand analysis. You are given the product identity (already determined); "
    "you do NOT re-identify it. Terms must be REAL, high-volume queries people actually type into a search "
    "box — short, keyword-style (2-4 words). Do NOT invent long descriptive noun phrases nobody types (they "
    "return zero search volume). ALWAYS include, in EVERY list, the product's ORIGINAL English/Latin-script "
    "names untranslated (they capture real demand and guard against bad transliterations).\n\n"
    "THE BRAND RULE decides what every term must contain, and it is not stylistic — the volumes of these "
    "terms are SUMMED into one product's demand score:\n"
    "  - A BRAND was given: EVERY term must name that brand, in whatever script local buyers use "
    "('cardhu 12', 'whisky cardhu', 'カーデュ 12年'). Vary the model/size/variant around the brand "
    "('KTM Duke', 'KTM 390', 'KTM 390 Duke'). NEVER the plain generic product name on its own "
    "('single malt whisky', 'motorcycle'): the whole category's search volume would be counted as this "
    "brand's demand. NEVER another brand's name.\n"
    "  - NO brand was given: the product is the CATEGORY. Use only generic names, never any brand — a "
    "branded term would make one brand's demand stand for the entire category.\n"
    "Return ONLY the structured result."
)


class EnrichmentClient:
    def __init__(self) -> None:
        # Provider + model resolved from .env for the `enrichment` site (default:
        # ollama / qwen3:14b). Fails fast if the chosen provider is misconfigured.
        self._provider = resolve_enrichment_llm()

    async def localized_markets(
        self,
        *,
        product_name: str,
        brand: str | None,
        category: str | None,
        countries: list[tuple[str, str | None]],
    ) -> list[MarketTerms]:
        """Generate a MarketTerms entry for each (ISO3, language) in ``countries``.
        Raises EnrichmentError on provider failure — the caller falls back to a
        deterministic entry so localized_markets is never left empty."""
        if not countries:
            return []
        product_line = f"{brand} {product_name}".strip() if brand else product_name
        country_lines = "\n".join(
            f"- {iso3} (primary search language: {lang})" if lang else f"- {iso3}" for iso3, lang in countries
        )
        prompt = (
            f"Product: {product_line}\n"
            f"Generic name: {product_name}\n"
            f"Brand: {brand or '(none)'}\n"
            f"Category: {category or '(unknown)'}\n\n"
            "Produce EXACTLY ONE markets entry for EACH of these countries (by ISO-3166 alpha-3):\n"
            f"{country_lines}\n\n"
            "For each country: `country` = the ISO3 code; `primary_name` = the single most-used real search "
            "term (local language); `search_terms` = a GENEROUS list of the REAL, high-volume queries a local "
            "buyer types — local-language colloquial/loanword names, spelling/spacing variants, and ALWAYS "
            "the original English/Latin-script names too, NEVER literal translations or long descriptive "
            "phrases; `excluded_terms` = look-alike terms that mean something ELSE; `confidence` = 0-1. "
            "Order search_terms primary-first.\n\n"
            + (
                f"EVERY search term must name the brand {brand!r} (in the local script), varying model/size/"
                "variant around it. NEVER the bare generic name and NEVER another brand: the volumes are "
                "summed into this brand's demand score."
                if brand
                else "This product is the CATEGORY, so no term may carry a brand name — a branded term would "
                "make one brand's demand stand for the whole category."
            )
        )
        try:
            result = await self._provider.complete(system=_SYSTEM, user=prompt, response_model=LocalizedMarketsResult)
        except LLMError as exc:
            raise EnrichmentError(f"Enrichment LLM call failed: {exc}") from exc
        if not isinstance(result, LocalizedMarketsResult):  # defensive; schema is provider-enforced
            raise EnrichmentError("Enrichment LLM returned an unexpected result type")
        return result.markets
