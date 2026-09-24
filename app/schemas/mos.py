import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class ComputeMosRequest(BaseModel):
    entity_key: uuid.UUID
    countries: list[str] = Field(..., min_length=1, description="ISO3 country codes")
    period_key: str | None = Field(None, description="'YYYY-MM'; defaults to the current month")
    # A marca desta análise. Nulo é "do produto" — e sem esta separação o MOS
    # da marca sobrescrevia o do produto na mesma linha (migration 048).
    brand_key: uuid.UUID | None = None


class MosResultResponse(BaseModel):
    id: uuid.UUID
    entity_key: uuid.UUID
    country: str
    period_type: str
    period_key: str
    mos: Decimal | None
    mos_before_cap: Decimal | None
    capped_by: str | None
    topsis_score: Decimal | None
    library_topsis_score: Decimal | None
    library_mos: Decimal | None
    mos_interpretation: str | None
    explanation: str | None
    availability_status: str
    excluded_reason: str | None
    computed_at: datetime


class ComputeMosResponse(BaseModel):
    results: list[MosResultResponse]
