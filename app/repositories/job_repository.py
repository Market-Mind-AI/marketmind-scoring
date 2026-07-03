import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models.job import Job
from app.db.models.job_task import JobTask
from app.domain.enums import JobStatus, JobTaskStatus
from app.utils.time import utc_now


class JobRepository:
    """Owns the race-dedup guarantee described in
    docs/marketmind-implementation-plan.md section 5: two callers asking for
    the same gap at the same time must not trigger two collection calls.

    Mechanism: ``find_active_task`` (SELECT ... FOR UPDATE SKIP LOCKED) is
    the fast, non-racy path most callers hit — it locks any pending/running
    task matching the gap so a concurrent caller doesn't grab it as free.
    The real backstop for the narrower race (two transactions both seeing
    "no active task" because neither has committed yet) is the partial
    unique index on job_tasks: ``create_task`` returns None on the resulting
    IntegrityError, and the caller (coverage_service) re-queries and attaches
    to the winner instead of dispatching a duplicate collection."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_job(self, *, requested_by: str | None, request_payload: dict[str, Any]) -> Job:
        job = Job(
            id=uuid.uuid4(),
            status=JobStatus.RUNNING,
            current_step=None,
            requested_by=requested_by,
            request_payload=request_payload,
            created_at=utc_now(),
        )
        self._session.add(job)
        await self._session.flush()
        return job

    async def get_job(self, job_id: uuid.UUID) -> Job | None:
        stmt = select(Job).where(Job.id == job_id).options(selectinload(Job.tasks))
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def finalize_job(self, job: Job, *, has_failed_task: bool) -> None:
        # Deliberately not reading job.tasks here: it's a lazy-loaded
        # relationship and this repository only ever constructs JobTask rows
        # directly (job_id set, not via job.tasks.append), so triggering a
        # lazy load on an AsyncSession would require an explicit
        # selectinload reload for no benefit — the caller already has the
        # per-gap outcome from processing each task.
        job.status = JobStatus.FAILED if has_failed_task else JobStatus.DONE
        job.finished_at = utc_now()
        await self._session.flush()

    async def find_active_task(
        self,
        *,
        source: str,
        entity_key: uuid.UUID | None,
        country: str,
        indicator_code: str,
        period_key: str,
    ) -> JobTask | None:
        stmt = (
            select(JobTask)
            .where(
                JobTask.source == source,
                JobTask.entity_key == entity_key,
                JobTask.country == country,
                JobTask.indicator_code == indicator_code,
                JobTask.period_key == period_key,
                JobTask.status.in_([JobTaskStatus.PENDING, JobTaskStatus.RUNNING]),
            )
            .with_for_update(skip_locked=True)
        )
        result = await self._session.execute(stmt)
        return result.scalars().first()

    async def create_task(
        self,
        *,
        job_id: uuid.UUID,
        source: str,
        entity_key: uuid.UUID | None,
        country: str,
        indicator_code: str,
        period_key: str,
    ) -> JobTask | None:
        task = JobTask(
            id=uuid.uuid4(),
            job_id=job_id,
            source=source,
            entity_key=entity_key,
            country=country,
            indicator_code=indicator_code,
            period_key=period_key,
            status=JobTaskStatus.PENDING,
            created_at=utc_now(),
        )
        self._session.add(task)
        try:
            await self._session.flush()
        except IntegrityError:
            await self._session.rollback()
            return None
        return task

    async def mark_skipped(
        self,
        *,
        job_id: uuid.UUID,
        source: str,
        entity_key: uuid.UUID | None,
        country: str,
        indicator_code: str,
        period_key: str,
    ) -> JobTask:
        now = utc_now()
        task = JobTask(
            id=uuid.uuid4(),
            job_id=job_id,
            source=source,
            entity_key=entity_key,
            country=country,
            indicator_code=indicator_code,
            period_key=period_key,
            status=JobTaskStatus.SKIPPED,
            created_at=now,
            finished_at=now,
        )
        self._session.add(task)
        await self._session.flush()
        return task

    async def mark_running(self, task: JobTask, *, ingestion_run_id: uuid.UUID) -> None:
        task.status = JobTaskStatus.RUNNING
        task.ingestion_run_id = ingestion_run_id
        await self._session.flush()

    async def mark_done(self, task: JobTask) -> None:
        task.status = JobTaskStatus.DONE
        task.finished_at = utc_now()
        await self._session.flush()

    async def mark_failed(self, task: JobTask, *, error_message: str) -> None:
        task.status = JobTaskStatus.FAILED
        task.finished_at = utc_now()
        task.error_message = error_message
        await self._session.flush()
