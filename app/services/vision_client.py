"""Vision provider for photo-based product identification (Fase 5,
app/services/product_identification_service.py). The actual LLM call goes
through the provider RESOLVER (app/services/llm) — provider (claude/gpt/ollama)
and model are chosen per the `vision` site from .env (default: ollama /
qwen3-vl:8b-instruct), so swapping is an env-only change. This module owns the
prompt + the structured schema; the provider is pure transport.

Structured output (``response_model``) guarantees a parseable, schema-valid
result — never free text the caller has to regex out — consistent with the
rest of this project's "no imputación por IA" ethos: the LLM identifies,
it never silently invents a field outside the declared schema.
"""

from pydantic import BaseModel, Field

from app.core.exceptions import MarketMindCoreError
from app.schemas.catalog import ImageMediaType
from app.services.llm import ImageInput, LLMError, resolve_vision_llm


class VisionError(MarketMindCoreError):
    """The vision provider call failed or returned something we can't use."""


class ProductNotIdentifiedError(MarketMindCoreError):
    """The input (image and/or text) did not identify a concrete product — the
    model reported product_found=false rather than fabricating one. Surfaced to
    the caller as a 422 so the frontend can ask for a photo or a real description
    instead of proceeding with a hallucinated product."""


class MarketTerms(BaseModel):
    """The real search vocabulary for the product in ONE country's market.
    Demand is measured from ABSOLUTE Google Ads search volume (not Google
    Trends), so these are the exact terms whose monthly search volumes get
    summed to the country's demand level — they must be terms local buyers
    genuinely type, not literal translations."""

    country: str = Field(description="ISO-3166 alpha-3 country code this market entry is for (e.g. 'PRT', 'USA').")
    search_language: str = Field(
        description=(
            "ISO 639-1 code of the language these search_terms are written in — the language people in this "
            "country actually search this product in ('pt' for Brazil, 'ar' for Saudi Arabia, 'en' for India). "
            "You already chose it by writing the terms; state it. Where a country searches in more than one, "
            "give the one THESE terms are in, which should be the one most buyers of THIS product use."
        )
    )
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
            "real demand — be generous, but every term must genuinely refer to THIS product. When the product "
            "has a brand, EVERY term must name that brand (in the local script) and never the bare generic "
            "name, which would count the whole category as this brand's demand; with no brand, no term may "
            "carry one. Order primary-first. "
            "Example (Portugal, air fryer): ['air fryer','fritadeira air fryer','fritadeira de ar quente',"
            "'fritadeira sem óleo']."
        )
    )
    excluded_terms: list[str] = Field(
        default_factory=list,
        description=(
            "Everything that must NOT be counted as demand to buy this. Matched as a substring against "
            "Google's related keywords, so give the shortest distinguishing form. Three kinds: a different "
            "product sharing the name ('yerba buena' = mint; 'suína' = pork); using it rather than buying it, "
            "written the way that language writes preparation ('na airfryer', 'no forno', 'receita'); and "
            "looking it up rather than shopping ('o que é', 'vs', 'wikipedia')."
        ),
    )
    confidence: float = Field(description="0-1 confidence in this market's terms; low values are flagged for review.")


class ProductIdentification(BaseModel):
    """Cap. 4/5 note: `estimated_price` is the one field in this whole
    pipeline that is an LLM guess rather than sourced from a real API —
    never scored, only carried as context (see product_identification_service.py
    and memory marketmind_mos_engine)."""

    product_found: bool = Field(
        default=True,
        description=(
            "TRUE only when the input actually identifies a specific product. Set it FALSE — and leave "
            "product_name empty — when there is NO identifiable product: no image was given AND the text does "
            "not name or describe a concrete product (e.g. it is empty, or just 'quero'/'hello'/'test'/a "
            "question with no product). NEVER invent or guess a product to fill this in — an absent product must "
            "be reported as product_found=false, not fabricated."
        ),
    )
    product_name: str = Field(
        description=(
            "Short, generic, commonly-searched canonical product name in plain ASCII (e.g. 'Iberian ham', "
            "'air fryer'). No brand, certification, breed/percentage, size, or parenthetical translations. "
            "Strip accents/diacritics (e.g. 'Jamon Iberico', not 'Jamón Ibérico'). Empty when product_found=false."
        )
    )
    brand: str | None
    category: str | None = Field(
        description=(
            "Short, generic product category in English, one level broader than `product_name` (e.g. "
            "'Charcuterie', 'Kitchen Appliances', 'Toys'). Always English regardless of the product's origin. "
            "If the item is an ACCESSORY, spare part, consumable, service or content FOR another product "
            "(a basket, liner, filter, cookbook, repair service), the category must say so — 'Kitchen "
            "Accessories', 'Spare Parts', 'Cookbooks', 'Services' — and NEVER the category of the product it "
            "belongs to. An air fryer basket is not a Kitchen Appliance."
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
        # Provider + model resolved from .env for the `vision` site (default:
        # ollama / qwen3-vl:8b-instruct). Constructing here fails fast if the
        # chosen provider is misconfigured (e.g. a missing API key).
        self._provider = resolve_vision_llm()

    async def identify_product(
        self,
        *,
        image_base64: str | None,
        image_media_type: ImageMediaType | None,
        hint_text: str | None,
        countries: list[str],
    ) -> ProductIdentification:
        # The image is optional: identify from the photo when present, otherwise
        # purely from the user's text description (hint_text). At least one must
        # be provided (enforced by the request schema + the guard below).
        if not image_base64 and not (hint_text and hint_text.strip()):
            raise VisionError("identify_product requires an image or a text description")
        country_list = ", ".join(countries) if countries else "(none provided)"
        subject = "this product" if image_base64 else "the product described below"
        instructions = (
            f"Identify {subject} for a cross-country market-demand analysis. "
            "CRITICAL: only identify a product that is ACTUALLY present in the input. If no image was given and "
            "the text does not name or describe a concrete product (e.g. it is empty or just 'quero', 'hello', "
            "'test', or a question with no product), set `product_found`=false, leave `product_name` empty, and "
            "return no markets — do NOT invent, guess, or default to any product. "
            "`product_name`: short, generic, commonly-searched canonical name, plain ASCII, no brand/size/"
            "certification (e.g. 'Iberian ham', 'air fryer'; 'Jamon Iberico', not 'Jamón Ibérico'). "
            "`category`: one level broader, always in English (e.g. 'Charcuterie', 'Kitchen Appliances'); an "
            "ACCESSORY, part, consumable, service or content FOR another product gets its OWN category "
            "('Kitchen Accessories', 'Spare Parts', 'Cookbooks', 'Services'), never the category of the product "
            "it belongs to. "
            "Put the brand only in `brand`; `product_name` stays generic. "
            "`markets`: provide EXACTLY ONE entry for EACH of these ISO-3166 alpha-3 countries: "
            f"{country_list}. For each country, `search_terms` is a GENEROUS list of the real terms a local "
            "buyer actually types for THIS product — genuine colloquial/regional/loanword names and spelling/"
            "spacing variants, NEVER word-for-word translations, and ALWAYS also include the original English/"
            "Latin-script names in every market's list. The system pulls each term's ABSOLUTE monthly "
            "search volume and SUMS them into ONE product's demand score, so more real terms = more captured "
            "demand (be generous, but every term must truly mean this product). THE BRAND DECIDES WHAT "
            "'this product' MEANS: if you identified a brand, EVERY term must name it, in whatever script "
            "local buyers use ('cardhu 12', 'whisky cardhu', 'カーデュ 12年'), varying model/size/variant "
            "around the brand — NEVER the bare generic name ('single malt whisky'), which would count the "
            "entire category's search volume as this brand's demand, and NEVER another brand. If there is no "
            "brand, the product IS the category: use only generic terms, never a branded one. "
            "Set `primary_name` to the single most-used term. "
            "These volumes are summed into a score answering 'should I sell this here', so `search_terms` "
            "must only be what someone types while deciding to BUY or looking for where to buy.\n"
            "`excluded_terms` is what must NOT be counted, and it has to be thorough — the terms are matched "
            "as substrings against Google's related keywords, so give the SHORTEST distinguishing form "
            "('suína', not 'picanha suína temperada'). Three kinds: (1) a DIFFERENT product sharing the name "
            "(pork 'picanha' is not beef picanha; 'yerba buena' is mint, not yerba mate; 'fritadeira de óleo' "
            "is an oil deep-fryer); (2) USING it rather than buying it, whoever searches it already owns it — "
            "name the ways preparation is written in THAT language, usually the method itself rather than a "
            "question ('na airfryer', 'no forno', 'na brasa', 'churrasco', 'receita'; for a device, 'como "
            "limpar', 'não liga', 'manual'); (3) LOOKING IT UP rather than shopping ('o que é', 'vs', "
            "'significado', 'wikipedia' and the local forms). Local language, lowercase. Be generous: a term "
            "wrongly excluded costs a little demand, a term wrongly counted inflates one market against all "
            "the others. Give a `confidence` (0-1) per market. Example — Brazil picanha: primary_name "
            "'picanha', search_terms ['picanha','picanha bovina','preço da picanha','picanha angus'], "
            "excluded_terms ['suína','de porco','na airfryer','no forno','na brasa','churrasco','receita']."
        )
        # With no image the text IS the product source, so label it accordingly.
        if hint_text and hint_text.strip():
            label = "Additional context from the user" if image_base64 else "Product description from the user"
            prompt = f"{instructions}\n\n{label}: {hint_text}"
        else:
            prompt = instructions
        image = (
            ImageInput(base64_data=image_base64, media_type=image_media_type)
            if image_base64 and image_media_type
            else None
        )
        try:
            result = await self._provider.complete(
                user=prompt,
                image=image,
                response_model=ProductIdentification,
            )
        except LLMError as exc:
            raise VisionError(f"Vision LLM call failed: {exc}") from exc
        if not isinstance(result, ProductIdentification):  # defensive; the schema is enforced by the provider
            raise VisionError("Vision LLM returned an unexpected result type")
        # Refuse hallucinated products: the model must report product_found=false
        # (not fabricate) when the input names no concrete product.
        if not result.product_found or not (result.product_name and result.product_name.strip()):
            raise ProductNotIdentifiedError(
                "Could not identify a product — add a photo or describe the product in the prompt."
            )
        return result
