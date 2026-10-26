import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class ComputeIndicatorsRequest(BaseModel):
    country: str = Field(..., min_length=3, max_length=3, description="ISO3 country code")
    entity_key: uuid.UUID | None = None
    # A marca desta análise, quando há. Decide QUAL vocabulário é medido — e é
    # a peça sem a qual a coleta usa os termos da marca e a medição lê os do
    # produto, que foi o que produziu MOS idêntico para "coffee" e para
    # "coffee + Juan Valdez" em 24/09/2026.
    brand_key: uuid.UUID | None = None
    # Obrigatório: quem pede é quem fixou o período da análise. Lido de um
    # relógio aqui, uma análise que atravessa a virada do mês mediria dois.
    period_key: str = Field(..., min_length=1, description="'YYYY-MM', the analysis period")
    demand_ceiling: float | None = Field(
        None,
        gt=0,
        description=(
            "The product's reference-market search volume, which Demand Volume is read against "
            "(score = volume / ceiling x 100). Omit it and the score falls back to a fixed 2,000,000 "
            "bound, which makes every real product look like almost no demand: a market with 182,540 "
            "monthly searches scores 9 instead of 34, and the macro dimensions then decide the ranking."
        ),
    )


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
