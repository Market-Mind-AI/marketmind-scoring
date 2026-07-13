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


class ProductIdentification(BaseModel):
    """Cap. 4/5 note: `estimated_price` is the one field in this whole
    pipeline that is an LLM guess rather than sourced from a real API —
    never scored, only carried as context (see product_identification_service.py
    and memory marketmind_mos_engine)."""

    product_name: str = Field(
        description=(
            "Short, generic, commonly-searched product name — the term a "
            "shopper would actually type into a search engine (e.g. 'Iberian "
            "ham', 'Serrano ham', 'air fryer'). This feeds a Google Trends "
            "query downstream, so an overly specific or descriptive title "
            "returns zero search volume even for a real, popular product. "
            "Never include brand, certification labels, breed/percentage "
            "claims, package size, or parenthetical translations here. "
            "Plain ASCII only — no accents or diacritics, even if the "
            "product's native-language name normally has them (e.g. "
            "'Jamon Iberico', not 'Jamón Ibérico'; 'Cafe', not 'Café')."
        )
    )
    brand: str | None
    category: str | None = Field(
        description=(
            "Short, generic product category in English, one level broader "
            "than `product_name` — the kind of label a store aisle or "
            "marketplace taxonomy would use (e.g. 'Charcuterie', 'Kitchen "
            "Appliances', 'Toys'). Always in English regardless of the "
            "product's own language or origin. This is the fallback search "
            "term used when `product_name` itself returns no Google Trends "
            "data, so it must be a real, broad, commonly-searched category — "
            "never a descriptive phrase, specification list, or marketing copy."
        )
    )
    localized_terms: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Map of {ISO-639-1 language code: LIST of real local market names}. "
            "For each language, list 2-4 terms that a local buyer ACTUALLY types "
            "into Google to find this product — the genuine colloquial/regional "
            "names, NEVER a word-for-word translation of `product_name`. This is "
            "the critical rule: the same product has a different real name in "
            "each market. Iberian ham is called 'presunto de porco preto' and "
            "'presunto pata negra' in Portugal (NOT the literal 'presunto "
            "ibérico'), 'jamón de bellota' / 'jamón pata negra' in Spain, "
            "'pata negra ham' in the English-speaking world. So give, e.g.: "
            "pt: ['presunto de porco preto','presunto pata negra','presunto "
            "ibérico'], es: ['jamón ibérico','jamón de bellota','jamón pata "
            "negra'], en: ['iberico ham','pata negra ham']. Order each list "
            "best-guess-first. The system compares ALL of them on Google Trends "
            "and lets real search volume decide the winner, so include every "
            "plausible local name rather than betting on one. Provide an entry "
            "for EVERY language: es, pt, en, de, fr, it, ja, zh, ko, ru, id, tr, "
            "ar, nl, pl, sv. Keep terms short and brand-free (no size/"
            "certification), in each language's own script/accents."
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
        self, *, image_base64: str, image_media_type: ImageMediaType, hint_text: str | None
    ) -> ProductIdentification:
        if self._client is None:
            raise VisionError("ANTHROPIC_API_KEY is not configured")

        instructions = (
            "Identify this product. `product_name` and `category` both feed "
            "Google Trends search terms downstream (category is the fallback "
            "when product_name alone returns no data), so both must be short, "
            "generic, commonly-searched terms — never a full marketing title, "
            "specification list, or descriptive phrase. "
            "`product_name`: the specific product a shopper would type (e.g. "
            "'Iberian ham', 'Serrano ham', 'air fryer'). Plain ASCII only — "
            "strip accents/diacritics even if the native-language name "
            "normally carries them (e.g. 'Jamon Iberico', not 'Jamón Ibérico'). "
            "`category`: one level broader, always in English regardless of "
            "the product's own language (e.g. 'Charcuterie', 'Kitchen "
            "Appliances', 'Toys'). "
            "Put the brand only in `brand`, never in `product_name` or "
            "`category`. "
            "`localized_terms`: for each of the languages es, pt, en, de, fr, "
            "it, ja, zh, ko, ru, id, tr, ar, nl, pl, sv, give a LIST of 2-4 "
            "genuine local market names a buyer in that language really types — "
            "the real colloquial/regional names, NOT word-for-word translations. "
            "Iberian ham in Portugal is 'presunto de porco preto' / 'presunto "
            "pata negra' (not the literal 'presunto ibérico'); in Spain 'jamón "
            "de bellota' / 'jamón pata negra'; in English 'pata negra ham'. "
            "Include every plausible local name — the system compares all of "
            "them on Google Trends and lets real search volume pick the winner, "
            "so this directly drives the demand score."
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
                max_tokens=1024,
                messages=[{"role": "user", "content": [image_block, text_block]}],
                output_format=ProductIdentification,
            )
        except AnthropicAPIError as exc:
            raise VisionError(f"Failed to call Anthropic vision API: {exc}") from exc

        if response.parsed_output is None:
            raise VisionError(f"Anthropic vision API returned no parseable result (stop_reason={response.stop_reason})")
        return response.parsed_output
