import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class ComputeIndicatorsRequest(BaseModel):
    country: str = Field(..., min_length=3, max_length=3, description="ISO3 country code")
    entity_key: uuid.UUID | None = None
    period_key: str | None = Field(None, description="'YYYY-MM'; defaults to the current month")


class IndicatorValueResponse(BaseModel):
    id: uuid.UUID
    entity_key: uuid.UUID | None
    country: str
    indicator_code: str
    period_type: str
    period_key: str
    source: str
    raw_value: Decimal | None
    normalized_score: Decimal | None
    direction: str | None
    calculation_window: str | None
    availability_status: str
    notes: str | None
    business_context: str | None
    interpretation_band: str | None
    explanation: str | None
    computed_at: datetime


class ComputeIndicatorsResponse(BaseModel):
    results: list[IndicatorValueResponse]
