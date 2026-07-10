import uuid
from typing import Literal

from pydantic import BaseModel, Field


class CatalogResolveRequest(BaseModel):
    text: str = Field(..., min_length=1)
    source: str = Field(..., min_length=1)
    category: str | None = None
    target_age_min: int | None = None
    target_age_max: int | None = None


class CatalogResolveResponse(BaseModel):
    entity_key: uuid.UUID
    canonical_title: str
    method: str  # "alias" | "trigram" | "embedding" | "created"
    # Exact meaning depends on method: trigram = pg_trgm similarity (0-1);
    # embedding = cosine similarity (1 - cosine_distance, 0-1); alias/created
    # have no match score (exact hit / nothing to compare against).
    match_score: float | None = None


class ProductAliasItem(BaseModel):
    alias_normalized: str
    source: str
    hit_count: int


class CatalogEntityResponse(BaseModel):
    entity_key: uuid.UUID
    canonical_title: str
    google_mid: str | None
    google_type: str | None
    category: str | None
    merged_into: uuid.UUID | None
    aliases: list[ProductAliasItem]


ImageMediaType = Literal["image/jpeg", "image/png", "image/gif", "image/webp"]


class IdentifyImageRequest(BaseModel):
    image_base64: str = Field(..., min_length=1)
    image_media_type: ImageMediaType
    hint_text: str | None = Field(None, description="Optional user-provided prompt/description alongside the photo")


class IdentifyImageResponse(BaseModel):
    entity_key: uuid.UUID
    canonical_title: str
    method: str  # "alias" | "trigram" | "embedding" | "created"
    # Same field names as AnalyzeRequest (app/schemas/analyze.py) on purpose —
    # the frontend passes these straight through, with whatever the user
    # corrected, into POST /analyze.
    product_name: str
    brand: str | None
    category: str | None
    localized_terms: dict[str, str] | None = None
    target_age_min: int | None
    target_age_max: int | None
    estimated_price: float | None
    price_currency: str | None
    confidence: float
