"""Enriches the identify-image JSON with the fields the vision model leaves
out — above all `localized_markets`, which must NEVER be empty (demand
collection needs each country's real search terms). Uses a TEXT LLM resolved
for the `enrichment` site (default ollama / qwen3:14b), NOT the vision model:
the local vision model is unreliable at producing real localized search
vocabulary. The prompt lives HERE (provider-agnostic). The LLM only proposes
the search terms from the already-identified product identity — it computes no
score and identifies nothing from pixels (Cap. 4 §4.0)."""

from pydantic import BaseModel, Field

from app.core.exceptions import MarketMindCoreError
from app.services.llm import LLMError, resolve_enrichment_llm
from app.services.vision_client import MarketTerms


class EnrichmentError(MarketMindCoreError):
    """The enrichment LLM call failed or returned something unusable."""


class LocalizedMarketsResult(BaseModel):
    """Structured-output wrapper: one MarketTerms per requested country."""

    markets: list[MarketTerms] = Field(default_factory=list)


_SYSTEM = (
    "You build the real online-search vocabulary a local buyer uses for a given product, per country, "
    "for a cross-country market-demand analysis. You are given the product identity (already determined); "
    "you do NOT re-identify it. For EACH requested country you return the genuine terms local buyers type — "
    "in that country's own language and colloquial usage, NEVER word-for-word translations — because each "
    "term's ABSOLUTE monthly Google Ads search volume is pulled and SUMMED into the country's demand level. "
    "CRITICAL: the terms must be REAL, high-volume queries people actually type into a search box — short, "
    "keyword-style (2-4 words), the way buyers really search. Prefer the BRAND together with the product's "
    "model/name and its common short variants (e.g. 'KTM Duke', 'KTM 390', 'KTM 390 Duke'), plus the plain "
    "generic product name people search. Do NOT invent long descriptive noun phrases nobody types (e.g. "
    "'motocicleta esportiva da marca KTM'): those return zero search volume and are useless. "
    "ALWAYS also include, in EVERY country's list, the product's ORIGINAL English / Latin-script brand and "
    "model names exactly as written worldwide (e.g. 'KTM 390 Duke', 'KTM 390') — buyers everywhere search "
    "these proper names untranslated, so they capture real local demand and guard against transliteration "
    "mistakes in non-Latin scripts. Return ONLY the structured result."
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
        """Generate a MarketTerms entry for each (ISO3, language) in `countries`.
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
            "term a local buyer types (in the local language); `search_terms` = a GENEROUS list of the REAL, "
            "high-volume queries a local buyer actually types for THIS product — short keyword-style terms "
            "(brand+model and its variants, plus the plain generic product name), genuine colloquial/regional/"
            "loanword names in the local language plus spelling/spacing variants, NEVER literal translations "
            "and NEVER long descriptive phrases nobody searches, AND ALWAYS include the original English/"
            "Latin-script brand+model names (e.g. 'KTM 390 Duke', 'KTM 390') in this list too; `excluded_terms` "
            "= look-alike terms that mean something ELSE in that market; `confidence` = 0-1. Order search_terms "
            "primary-first. Every term must genuinely refer to this exact product."
        )
        try:
            result = await self._provider.complete(system=_SYSTEM, user=prompt, response_model=LocalizedMarketsResult)
        except LLMError as exc:
            raise EnrichmentError(f"Enrichment LLM call failed: {exc}") from exc
        if not isinstance(result, LocalizedMarketsResult):  # defensive; schema is provider-enforced
            raise EnrichmentError("Enrichment LLM returned an unexpected result type")
        return result.markets
