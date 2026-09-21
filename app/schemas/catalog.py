import uuid
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class CatalogResolveRequest(BaseModel):
    # `text` é o nome GENÉRICO do produto. A marca vem separada: ela é
    # acumulada na entidade e vira um alias `marca + nome`, mas não participa
    # da decisão de identidade (ver catalog_service.resolve).
    text: str = Field(..., min_length=1)
    source: str = Field(..., min_length=1)
    brand: str | None = None
    category: str | None = None
    target_age_min: int | None = None
    target_age_max: int | None = None


class CatalogResolveResponse(BaseModel):
    entity_key: uuid.UUID
    # Com marca, o título carrega a marca ("Cardhu single malt Scotch whisky"):
    # é a identidade da entidade que a análise mede, e é isso que o relatório
    # mostra. `brand` e `generic_title` vêm separados para o frontend poder
    # exibir "Produto: X · Marca: Y" sem ter que fatiar string.
    canonical_title: str
    brand: str | None = None
    generic_title: str | None = Field(
        default=None,
        description=(
            "Título da entidade-família (a categoria genérica) quando esta entidade é de uma marca. "
            "Nulo numa entidade genérica, que é a própria família."
        ),
    )
    method: str  # "alias" | "trigram" | "llm" | "created"
    # Exact meaning depends on method: trigram = pg_trgm similarity (0-1);
    # trigram = similaridade pg_trgm (0-1); alias/llm/created
    # have no match score (exact hit / nothing to compare against).
    match_score: float | None = None


class ProductAliasItem(BaseModel):
    alias_normalized: str
    source: str
    hit_count: int


class CatalogEntityResponse(BaseModel):
    entity_key: uuid.UUID
    canonical_title: str
    category: str | None
    # A marca como PARTIÇÃO da identidade (migration 031) e a família genérica.
    # Expostos porque são o que torna a entidade auditável de fora: foi olhando
    # esta rota que se viu a entidade chamada `single malt Scotch whisky` com
    # vocabulário 100% Cardhu.
    brand: str | None = None
    generic_entity_key: uuid.UUID | None = None
    # SUPERADO por `brand` (ver ProductCatalog.brands): mantido enquanto o
    # reparo do catálogo não roda, porque é o único ponteiro do estado legado.
    brands: list[str] = []
    # Nunca escondido: uma entidade aposentada (pelo split de marca, por
    # exemplo) diz para onde foi, e quem lê decide se segue o ponteiro.
    merged_into: uuid.UUID | None = None
    aliases: list[ProductAliasItem]


ImageMediaType = Literal["image/jpeg", "image/png", "image/gif", "image/webp"]


class LocalizedMarket(BaseModel):
    """One country's curated search vocabulary, mirroring a value of
    product_catalog.localized_markets (keyed by ISO3, so `country` lives in
    the key, not here). Produced by the identify-image LLM and user-reviewable
    before /analyze."""

    primary_name: str
    formal_name: str | None = None
    search_terms: list[str] = Field(default_factory=list)
    excluded_terms: list[str] = Field(default_factory=list)
    confidence: float


class ReferenceMarket(LocalizedMarket):
    """One candidate biggest-market, mirroring an entry of
    product_catalog.reference_candidates. Carries its own `country` (ISO3),
    unlike LocalizedMarket where the country is the dict key. The analysis
    measures each candidate's real search volume; the largest is the Demand
    Volume ruler ceiling (Cap. 4 §4.2.1). Never scored/ranked itself — context
    only, explained by the final LLM."""

    country: str


class IdentifyImageRequest(BaseModel):
    # Image is now OPTIONAL: the product can be identified from the photo OR from
    # the text prompt (hint_text). At least one must be present (validated below).
    image_base64: str | None = Field(None, min_length=1)
    image_media_type: ImageMediaType | None = None
    hint_text: str | None = Field(None, description="User-provided prompt/description of the product")
    countries: list[str] = Field(
        ..., min_length=1, description="ISO3 country codes to identify per-market search vocabulary for"
    )

    @model_validator(mode="after")
    def _require_image_or_text(self) -> "IdentifyImageRequest":
        has_image = bool(self.image_base64)
        has_text = bool(self.hint_text and self.hint_text.strip())
        if not has_image and not has_text:
            raise ValueError("Provide a product image or a text description (at least one is required)")
        if has_image and self.image_media_type is None:
            raise ValueError("image_media_type is required when an image is provided")
        return self


class IdentifyImageResponse(BaseModel):
    entity_key: uuid.UUID
    canonical_title: str
    method: str  # "alias" | "trigram" | "llm" | "created"
    # Same field names as AnalyzeRequest (app/schemas/analyze.py) on purpose —
    # the frontend passes these straight through, with whatever the user
    # corrected, into POST /analyze.
    product_name: str
    brand: str | None
    category: str | None
    # Per-COUNTRY curated search terms, keyed by ISO3 — the shape persisted to
    # product_catalog.localized_markets. Replaces the old {lang: [names]}
    # `localized_terms`.
    localized_markets: dict[str, LocalizedMarket] | None = None
    reference_candidates: list[ReferenceMarket] | None = None
    target_age_min: int | None
    target_age_max: int | None
    estimated_price: float | None
    price_currency: str | None
    confidence: float
