"""Vision provider for photo-based product identification (Fase 5,
app/services/product_identification_service.py). Unlike embedding_client.py
(OpenAI via raw httpx, a stopgap because Anthropic has no first-party
embeddings endpoint), Anthropic *does* have a first-party vision endpoint —
uses the official `anthropic` SDK directly, no stopgap needed. Isolated
behind this one class so a provider swap stays a single-file change.

Structured output (``output_format``) guarantees a parseable, schema-valid
result — never free text the caller has to regex out — consistent with the
rest of this project's "no imputación por IA" ethos: the LLM identifies,
it never silently invents a field outside the declared schema.
"""

from anthropic import APIError as AnthropicAPIError
from anthropic import AsyncAnthropic
from anthropic.types import ImageBlockParam, TextBlockParam
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.exceptions import MarketMindCoreError
from app.schemas.catalog import ImageMediaType


class VisionError(MarketMindCoreError):
    """The vision provider call failed or returned something we can't use."""


class MarketTerms(BaseModel):
    """The real search vocabulary for the product in ONE country's market.
    Demand is measured from ABSOLUTE Google Ads search volume (not Google
    Trends), so these are the exact terms whose monthly search volumes get
    summed to the country's demand level — they must be terms local buyers
    genuinely type, not literal translations."""

    country: str = Field(description="ISO-3166 alpha-3 country code this market entry is for (e.g. 'PRT', 'USA').")
    primary_name: str = Field(
        description="The single most-used real search term for this product in this country (a local buyer's default)."
    )
    formal_name: str | None = Field(
        default=None, description="Optional formal/dictionary name for the product in this market, if different."
    )
    search_terms: list[str] = Field(
        description=(
            "GENEROUS list of every real search term a local buyer plausibly types for this product in this "
            "country — the genuine colloquial/regional/loanword names, NEVER word-for-word translations. Include "
            "spelling/spacing variants (e.g. 'air fryer' AND 'airfryer'). The system pulls each term's ABSOLUTE "
            "monthly search volume and SUMS them for the demand level, so listing more real terms captures more "
            "real demand — be generous, but every term must genuinely refer to THIS product. Order primary-first. "
            "Example (Portugal, air fryer): ['air fryer','fritadeira air fryer','fritadeira de ar quente',"
            "'fritadeira sem óleo']."
        )
    )
    excluded_terms: list[str] = Field(
        default_factory=list,
        description=(
            "Ambiguous or wrong-meaning terms to EXCLUDE, so they never pollute the volume. List any term that "
            "looks related but denotes a different product or concept in this market (e.g. for yerba mate: "
            "'yerba buena' = mint; for air fryer: 'fritadeira de óleo' = oil deep-fryer)."
        ),
    )
    confidence: float = Field(description="0-1 confidence in this market's terms; low values are flagged for review.")


class ProductIdentification(BaseModel):
    """Cap. 4/5 note: `estimated_price` is the one field in this whole
    pipeline that is an LLM guess rather than sourced from a real API —
    never scored, only carried as context (see product_identification_service.py
    and memory marketmind_mos_engine)."""

    product_name: str = Field(
        description=(
            "Short, generic, commonly-searched canonical product name in plain ASCII (e.g. 'Iberian ham', "
            "'air fryer'). No brand, certification, breed/percentage, size, or parenthetical translations. "
            "Strip accents/diacritics (e.g. 'Jamon Iberico', not 'Jamón Ibérico')."
        )
    )
    brand: str | None
    category: str | None = Field(
        description=(
            "Short, generic product category in English, one level broader than `product_name` (e.g. "
            "'Charcuterie', 'Kitchen Appliances', 'Toys'). Always English regardless of the product's origin."
        )
    )
    markets: list[MarketTerms] = Field(
        default_factory=list,
        description=(
            "One entry per requested country (by ISO-3166 alpha-3), each with that country's real search "
            "vocabulary. This is the critical field: the same product has a DIFFERENT real name per market "
            "(Iberian ham = 'presunto pata negra' in Portugal, 'jamón de bellota' in Spain, 'pata negra ham' "
            "in English). Provide a `markets` entry for EVERY country the caller lists — no more, no less."
        ),
    )
    target_age_min: int | None
    target_age_max: int | None
    estimated_price: float | None
    price_currency: str | None
    confidence: float


class VisionClient:
    def __init__(self) -> None:
        self._client = AsyncAnthropic(api_key=settings.anthropic_api_key) if settings.anthropic_api_key else None

    async def identify_product(
        self,
        *,
        image_base64: str,
        image_media_type: ImageMediaType,
        hint_text: str | None,
        countries: list[str],
    ) -> ProductIdentification:
        if self._client is None:
            raise VisionError("ANTHROPIC_API_KEY is not configured")

        country_list = ", ".join(countries) if countries else "(none provided)"
        instructions = (
            "Identify this product for a cross-country market-demand analysis. "
            "`product_name`: short, generic, commonly-searched canonical name, plain ASCII, no brand/size/"
            "certification (e.g. 'Iberian ham', 'air fryer'; 'Jamon Iberico', not 'Jamón Ibérico'). "
            "`category`: one level broader, always in English (e.g. 'Charcuterie', 'Kitchen Appliances'). "
            "Put the brand only in `brand`. "
            "`markets`: provide EXACTLY ONE entry for EACH of these ISO-3166 alpha-3 countries: "
            f"{country_list}. For each country, `search_terms` is a GENEROUS list of the real terms a local "
            "buyer actually types for THIS product — genuine colloquial/regional/loanword names and spelling/"
            "spacing variants, NEVER word-for-word translations. The system pulls each term's ABSOLUTE monthly "
            "search volume and SUMS them, so more real terms = more captured demand (be generous, but every "
            "term must truly mean this product). Set `primary_name` to the single most-used term, and use "
            "`excluded_terms` to list look-alike terms that mean something ELSE in that market (e.g. yerba mate "
            "vs 'yerba buena'=mint; air fryer vs 'fritadeira de óleo'=oil deep-fryer), so they never pollute "
            "the volume. Give a `confidence` (0-1) per market. Example — Portugal air fryer: primary_name "
            "'air fryer', search_terms ['air fryer','fritadeira air fryer','fritadeira de ar quente',"
            "'fritadeira sem óleo'], excluded_terms ['fritadeira de óleo']."
        )
        prompt = f"{instructions}\n\nAdditional context from the user: {hint_text}" if hint_text else instructions
        image_block: ImageBlockParam = {
            "type": "image",
            "source": {"type": "base64", "media_type": image_media_type, "data": image_base64},
        }
        text_block: TextBlockParam = {"type": "text", "text": prompt}
        try:
            response = await self._client.messages.parse(
                model=settings.anthropic_vision_model,
                # Generous budget: the per-country `markets` (real search terms
                # for every requested country) is a large structured payload —
                # 1024 forced the model to return an empty markets list.
                max_tokens=8192,
                messages=[{"role": "user", "content": [image_block, text_block]}],
                output_format=ProductIdentification,
            )
        except AnthropicAPIError as exc:
            raise VisionError(f"Failed to call Anthropic vision API: {exc}") from exc

        if response.parsed_output is None:
            raise VisionError(f"Anthropic vision API returned no parseable result (stop_reason={response.stop_reason})")
        return response.parsed_output
