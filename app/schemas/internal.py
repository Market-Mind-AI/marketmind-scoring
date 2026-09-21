"""Request and response shapes for the /internal routes — the tools the
orchestrator drives an analysis with.

Every one of them takes `period_key` explicitly rather than reading a clock.
Two calls that belong to the same analysis must agree on the period even when
the analysis runs across midnight or is resumed a day later, and a tool that
decides its own period cannot be replayed to the same answer.
"""

import uuid
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field


class RegisterAnalysisRequest(BaseModel):
    entity_key: uuid.UUID | None = None
    canonical_title: str | None = None
    countries: list[str] = Field(..., min_length=1)
    estimated_price: Decimal | None = None
    price_currency: str | None = None
    user_prompt: str | None = None


class RegisteredAnalysisResponse(BaseModel):
    request_id: uuid.UUID
    status: str


class ProgressRequest(BaseModel):
    # None clears the note. The heartbeat is stamped either way: reporting that
    # nothing is happening is still a sign of life, and the sweep that resumes
    # dead analyses reads the heartbeat, not the note.
    message: str | None = None


class ProgressResponse(BaseModel):
    request_id: uuid.UUID
    progress: str | None = None


class CompleteAnalysisRequest(BaseModel):
    result: list[dict[str, Any]] = Field(..., min_length=1)
    breakdown: dict[str, Any] | None = None
    explanation: str | None = None


class FailAnalysisRequest(BaseModel):
    error_message: str = Field(..., min_length=1)


class AnalysisStateResponse(BaseModel):
    request_id: uuid.UUID
    status: str


class AnalysisDetailResponse(BaseModel):
    """Everything a resumed run needs to pick the analysis back up.

    A resumed run must not re-decide what was already decided: which product,
    which countries, what the buyer asked. Reading them back is what makes
    resuming cheap, since every step is idempotent and re-running is the whole
    recovery strategy.
    """

    request_id: uuid.UUID
    status: str
    entity_key: uuid.UUID | None = None
    canonical_title: str | None = None
    countries: list[str] = []
    user_prompt: str | None = None
    progress: str | None = None
    explanation: str | None = None
    error_message: str | None = None
    result: list[dict[str, Any]] | None = None


class CalibrateWeightsRequest(BaseModel):
    period_key: str = Field(..., min_length=1)


class CalibrateWeightsResponse(BaseModel):
    period_key: str
    weights: dict[str, float]


class EvidenceRequest(BaseModel):
    entity_key: uuid.UUID
    countries: list[str] = Field(..., min_length=1)
    period_key: str = Field(..., min_length=1)
    demand_ruler: dict[str, Any] | None = None


class EvidenceResponse(BaseModel):
    # None when no country in the request has a stored MOS for that period.
    # Absent evidence is reported as absent, never as an empty packet that
    # would read downstream as "nothing to say about this market".
    packet: dict[str, Any] | None = None


class NarrateRequest(BaseModel):
    packet: dict[str, Any]
    product_name: str
    user_prompt: str | None = None


class NarrateResponse(BaseModel):
    # None when the model failed. Best-effort by design: the numbers are the
    # answer, the prose is a reading of them, and a missing reading never
    # invalidates the answer.
    narrative: str | None = None
