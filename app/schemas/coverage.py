import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.domain.enums import DataSource, JobTaskStatus


class CoverageGap(BaseModel):
    """One unit of coverage the caller wants checked and, if missing,
    collected. Maps 1:1 to a coverage_index row and, if collection is
    needed, to exactly one job_task / one external ingestion run.

    ``collection_params`` carries whatever the target source needs beyond
    country/indicator_code to build its request (e.g. ``keyword`` for
    PyTrends, ``start_year``/``end_year`` for WorldBank/UNData) — see
    app/services/collection_client.py for the exact keys per source. The
    Core does not resolve these from the product catalog yet (Fase 2), so
    callers must supply them explicitly."""

    source: DataSource
    entity_key: uuid.UUID | None = None
    country: str = Field(..., min_length=3, max_length=3, description="ISO3 country code")
    indicator_code: str = Field(..., min_length=1)
    period_key: str = Field(..., min_length=1)
    is_closed: bool = False
    ttl_days: int | None = Field(None, description="Freshness window in days for this gap, when the caller wants one")
    collection_params: dict[str, Any] = Field(default_factory=dict)
    force: bool = Field(
        False,
        description=(
            "Collect even when the coverage_index row is still fresh. Set by callers that detect "
            "staleness the keyword-agnostic coverage_index can't see (e.g. the per-country PyTrends "
            "series exists but for a different/old winning keyword) — see analysis_service."
        ),
    )


class GapStatus(BaseModel):
    source: DataSource
    entity_key: uuid.UUID | None
    country: str
    indicator_code: str
    period_key: str
    covered: bool
    last_fetched_at: datetime | None = None


class CoverageCheckRequest(BaseModel):
    gaps: list[CoverageGap] = Field(..., min_length=1)


class CoverageCheckResponse(BaseModel):
    results: list[GapStatus]


class JobTaskResult(BaseModel):
    id: uuid.UUID
    source: DataSource
    entity_key: uuid.UUID | None
    country: str
    indicator_code: str
    period_key: str
    status: JobTaskStatus
    ingestion_run_id: uuid.UUID | None
    error_message: str | None


class CreateJobRequest(BaseModel):
    requested_by: str | None = None
    gaps: list[CoverageGap] = Field(..., min_length=1)


class JobResponse(BaseModel):
    id: uuid.UUID
    status: str
    tasks: list[JobTaskResult]


class JobDetailResponse(BaseModel):
    id: uuid.UUID
    status: str
    current_step: str | None
    requested_by: str | None
    created_at: datetime
    finished_at: datetime | None
    tasks: list[JobTaskResult]
