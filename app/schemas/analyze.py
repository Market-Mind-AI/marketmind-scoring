import uuid
from typing import Any

from pydantic import BaseModel, Field


class AnalyzeRequest(BaseModel):
    # Same field names as IdentifyImageResponse (app/schemas/catalog.py) on
    # purpose — the frontend passes these through unchanged, with whatever
    # the user corrected, from POST /catalog/identify-image.
    product_name: str = Field(..., min_length=1)
    brand: str | None = None
    category: str | None = None
    localized_terms: dict[str, list[str]] | None = Field(
        default=None,
        description="{lang: [real local market names]} from identify-image — per-country keyword localization",
    )
    target_age_min: int | None = None
    target_age_max: int | None = None
    estimated_price: float | None = None
    price_currency: str | None = None
    countries: list[str] = Field(..., min_length=1, description="ISO3 country codes")


class AnalyzeAcceptedResponse(BaseModel):
    request_id: uuid.UUID
    status: str  # "pending"


class AnalysisStatusResponse(BaseModel):
    request_id: uuid.UUID
    status: str  # "pending" | "running" | "done" | "failed"
    canonical_title: str | None
    result: list[dict[str, Any]] | None
    error_message: str | None
