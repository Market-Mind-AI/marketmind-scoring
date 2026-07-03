import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.repositories.job_repository import JobRepository
from app.schemas.coverage import (
    CoverageCheckRequest,
    CoverageCheckResponse,
    CreateJobRequest,
    JobDetailResponse,
    JobResponse,
    JobTaskResult,
)
from app.services.coverage_service import CoverageService, job_task_to_result

router = APIRouter()


def get_coverage_service(session: AsyncSession = Depends(get_session)) -> CoverageService:
    return CoverageService(session)


@router.post(
    "/coverage/check",
    response_model=CoverageCheckResponse,
    summary="Check coverage (read-only)",
    description=(
        "Reports which of the given gaps are already covered (fresh in "
        "coverage_index) without dispatching any collection. Callers that "
        "need to decide sync-vs-async handling before committing to a "
        "collection round-trip should call this first."
    ),
)
async def check_coverage(
    request: CoverageCheckRequest, service: CoverageService = Depends(get_coverage_service)
) -> CoverageCheckResponse:
    results = await service.check_coverage(request.gaps)
    return CoverageCheckResponse(results=results)


@router.post(
    "/jobs",
    response_model=JobResponse,
    summary="Check coverage and collect what's missing",
    description=(
        "For each gap: skips it if already fresh, attaches to an in-flight "
        "task if another caller is already collecting the same gap "
        "(race dedup), or dispatches collection to the right sibling "
        "service and polls it to completion. Synchronous for Fase 1 — "
        "Fase 5 decides when this needs to move behind 202+SSE instead."
    ),
)
async def create_job(
    request: CreateJobRequest, service: CoverageService = Depends(get_coverage_service)
) -> JobResponse:
    job_id, results = await service.check_or_collect(request.gaps, requested_by=request.requested_by)
    status = "failed" if any(r.status == "failed" for r in results) else "done"
    return JobResponse(id=job_id, status=status, tasks=results)


@router.get(
    "/jobs/{job_id}",
    response_model=JobDetailResponse,
    summary="Get job status",
    description="Polling endpoint for a job dispatched via POST /jobs.",
)
async def get_job(job_id: str, session: AsyncSession = Depends(get_session)) -> JobDetailResponse:
    try:
        parsed_id = uuid.UUID(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="job_id must be a UUID") from exc

    job = await JobRepository(session).get_job(parsed_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    tasks: list[JobTaskResult] = [job_task_to_result(task) for task in job.tasks]
    return JobDetailResponse(
        id=job.id,
        status=job.status,
        current_step=job.current_step,
        requested_by=job.requested_by,
        created_at=job.created_at,
        finished_at=job.finished_at,
        tasks=tasks,
    )
