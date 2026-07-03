import uuid

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
