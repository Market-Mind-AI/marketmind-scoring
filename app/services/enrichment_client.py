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
from app.prompts import prompt_text
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
    "('KTM Duke', 'KTM 390', 'KTM 390 Duke') — unless the product name carries a MODEL: then EVERY "
    "term names that model too and never another one, since another model's volume is another "
    "product's demand. NEVER the plain generic product name on its own "
    "('single malt whisky', 'motorcycle'): the whole category's search volume would be counted as this "
    "brand's demand. NEVER another brand's name.\n"
    "  - NO brand was given: use only generic names, never any brand — a branded term would make one "
    "brand's demand stand for the entire category.\n\n"
    "THE SPECIFICITY RULE is the same failure from the other side, and it applies with or without a "
    "brand: every term must denote THE SAME PRODUCT you were given. Dropping a qualifier that changes "
    "what the product IS turns the measurement into a different, larger market.\n"
    "  - 'ostrich feather duster' -> 'espanador de penas de avestruz', NEVER plain 'espanador' (every "
    "duster ever made).\n"
    "  - 'instant coffee' -> 'café solúvel', NEVER plain 'café'.\n"
    "  - 'extra virgin olive oil' -> 'aceite de oliva virgen extra', NEVER plain 'aceite de oliva'.\n"
    "A term that is the product's broader category is as wrong as another brand's name, and for the "
    "identical reason: its volume is summed into THIS product's demand score.\n"
    "When the specific term genuinely has little search volume, that is the ANSWER — a real product "
    "with little demand — and the analysis exists to report it. Reaching for the broader word to find "
    "a bigger number measures something nobody asked about.\n\n"
    "Return ONLY the structured result."
)

# How to fill each entry. Fixed, so it sits with the system prompt and is
# cached with it; the product and its countries go in the user message.
_TASK = (
    "For each country: `country` = the ISO3 code; `primary_name` = the single most-used real search "
    "term (local language); `search_terms` = a GENEROUS list of the REAL, high-volume queries a local "
    "buyer types — local-language colloquial/loanword names, spelling/spacing variants, and ALWAYS "
    "the original English/Latin-script names too, NEVER literal translations or long descriptive "
    "phrases; `excluded_terms` = everything that must NOT be counted; `confidence` = 0-1. "
    "Order search_terms primary-first.\n\n"
    "WHAT COUNTS AS DEMAND. These volumes are summed into a score that answers 'should I sell "
    "this here'. Only include a term someone types while deciding to BUY or looking for where "
    "to buy — the product name, its variants, sizes, grades, 'price', 'near me', 'buy'.\n\n"
    "WHAT MUST BE EXCLUDED, and this is where the list has to be thorough. `excluded_terms` are "
    "matched as substrings against Google's related keywords, so put the SHORTEST distinguishing "
    "form (the word 'suína', not 'picanha suína temperada'). Three kinds:\n"
    "  1. A DIFFERENT PRODUCT that shares the name. Pork 'picanha' is not beef picanha; a pepper "
    "called 'picana' is not a cut of meat.\n"
    "  2. USING it, not buying it — whoever searches this already owns it. Name the ways people "
    "write preparation in THIS language, which is usually the method itself rather than a "
    "question: for Portuguese meat that is 'na airfryer', 'no forno', 'na brasa', 'no espeto', "
    "'churrasco', 'receita'; for a device it would be 'como limpar', 'não liga', 'manual'.\n"
    "  3. LOOKING IT UP rather than shopping: 'o que é', 'vs', 'significado', 'wikipedia', and "
    "the local forms of those.\n"
    "Write them in the local language, lowercase, without accents where the language allows both "
    "spellings. Be generous: a term wrongly excluded costs a little demand, while a term wrongly "
    "counted inflates a market against every other one.\n\n"
    "ANTAGONIST PRODUCTS — `contrast_attributes` and `antagonist_terms`. These decide product "
    "IDENTITY, not demand: the catalog compares a new query against what this product IS and "
    "against these, and files the query under whichever side it is closer to. So they must be "
    "MINIMAL PAIRS. First list the defining attributes (usually 1-3) whose flip turns this product "
    "into ANOTHER real, commonly searched product — an espresso machine has several: 'brewing: pump "
    "pressure vs drip filter', 'brewing: pump pressure vs stovetop moka', 'format: machine vs "
    "capsule system'; a curl cream has 'result: define curls vs straighten' and 'texture: cream vs "
    "gel'. Then, per country, list for EACH attribute the full names local buyers search for the "
    "product with that one attribute flipped and nothing else changed ('fritadeira a óleo', never "
    "just 'óleo'), generic, in the local language.\n"
    "The test for every term: does it sit on the OPPOSITE side of one listed attribute? A product "
    "that shares this product's value is NOT an antagonist, however close it is — oat milk is also "
    "lactose-free, a toaster oven also cooks with hot air, a convection oven is not a fryer at all. "
    "Neighbours in the category, accessories, and broader or narrower versions of the product are "
    "NOT antagonists either: they belong, if anywhere, in excluded_terms. Usage, recipe and lookup "
    "words stay in excluded_terms too. A product with no real minimal pair gets empty lists — an "
    "empty list is a correct answer, a polluted one is not.\n\n"
    "When the identity lists `Defining attributes`, every search term keeps all of them. When it lists "
    "`Antagonist products`, none of them is ever a search term, and they are where `antagonist_terms` "
    "and `excluded_terms` start from — in the local language."
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
        defining_attributes: list[str] | None = None,
        antagonist_products: list[str] | None = None,
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
            f"Name: {product_name}\n"
            f"Brand: {brand or '(none)'}\n"
            f"Category: {category or '(unknown)'}\n"
            f"Defining attributes: {'; '.join(defining_attributes or []) or '(none given)'}\n"
            f"Antagonist products: {'; '.join(antagonist_products or []) or '(none given)'}\n\n"
            "Produce EXACTLY ONE markets entry for EACH of these countries (by ISO-3166 alpha-3):\n"
            f"{country_lines}"
        )
        # The brand rule is per request, so it goes after the cache breakpoint
        # (see LLMProvider): the rest of the instructions repeat on every call.
        brand_rule = (
            f"EVERY search term must name the brand {brand!r} (in the local script), varying model/size/"
            "variant around it — unless the product name carries a model, which then every term names too. "
            "NEVER the bare generic name and NEVER another brand: the volumes are summed into this brand's "
            "demand score."
            if brand
            else "This product is the CATEGORY, so no term may carry a brand name — a branded term would "
            "make one brand's demand stand for the whole category."
        )
        try:
            result = await self._provider.complete(
                system=f"{prompt_text('enrichment-system', _SYSTEM)}\n\n{_TASK}",
                system_tail=brand_rule,
                user=prompt,
                response_model=LocalizedMarketsResult,
            )
        except LLMError as exc:
            raise EnrichmentError(f"Enrichment LLM call failed: {exc}") from exc
        if not isinstance(result, LocalizedMarketsResult):  # defensive; schema is provider-enforced
            raise EnrichmentError("Enrichment LLM returned an unexpected result type")
        return result.markets
