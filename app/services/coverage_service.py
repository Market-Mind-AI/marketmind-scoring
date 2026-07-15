import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import MarketMindCoreError
from app.core.logging import logger
from app.db.models.job_task import JobTask
from app.domain.enums import DataSource, IngestionRunStatus, JobTaskStatus
from app.repositories.country_repository import CountryRepository
from app.repositories.coverage_repository import CoverageRepository
from app.repositories.job_repository import JobRepository
from app.schemas.coverage import CoverageGap, GapStatus, JobTaskResult
from app.services.collection_client import CollectionClient, CollectionRequest

# PyTrends indicators collected worldwide (no country), dispatched under the
# sentinel ISO3 "WLD" — see analysis_service._ensure_worldwide_demand_coverage
# and collection_client._build_pytrends_request.
_COUNTRYLESS_INDICATORS = frozenset({"interest_by_country"})
WORLD_SENTINEL_ISO3 = "WLD"


class CoverageService:
    """Fase 1: coverage detection + on-demand collection dispatch. Does not
    know what an indicator *means* (that's Fase 3/4) — it only knows whether
    a (source, entity_key, country, indicator_code, period_key) gap is
    already covered and, if not, how to ask the right sibling service to
    collect it and wait for the result. See docs/marketmind-implementation-plan.md
    section 5."""

    def __init__(self, session: AsyncSession, collection_client: CollectionClient | None = None) -> None:
        self._session = session
        self._coverage_repo = CoverageRepository(session)
        self._job_repo = JobRepository(session)
        self._country_repo = CountryRepository(session)
        self._collection_client = collection_client or CollectionClient()

    async def check_coverage(self, gaps: list[CoverageGap]) -> list[GapStatus]:
        """Pure read — never dispatches collection. Used by callers that
        want to know what's missing before deciding sync vs async handling
        (see Fase 5 /analyze)."""
        results = []
        for gap in gaps:
            row = await self._coverage_repo.get(
                gap.source.value, gap.entity_key, gap.country, gap.indicator_code, gap.period_key
            )
            results.append(
                GapStatus(
                    source=gap.source,
                    entity_key=gap.entity_key,
                    country=gap.country,
                    indicator_code=gap.indicator_code,
                    period_key=gap.period_key,
                    covered=self._coverage_repo.is_fresh(row),
                    last_fetched_at=row.last_fetched_at if row else None,
                )
            )
        return results

    async def check_or_collect(
        self, gaps: list[CoverageGap], *, requested_by: str | None
    ) -> tuple[uuid.UUID, list[JobTaskResult]]:
        """Creates a Job, resolves every gap (skip if already fresh, attach
        if already in flight elsewhere, otherwise dispatch + poll to
        completion), and finalizes the job. Fully synchronous for Fase 1 —
        Fase 5 decides when this needs to move behind 202+SSE instead."""
        job = await self._job_repo.create_job(
            requested_by=requested_by,
            request_payload={"gaps": [gap.model_dump(mode="json") for gap in gaps]},
        )
        job_id = job.id
        await self._session.commit()

        # Committed once per gap (not once for the whole job): each task's
        # outcome must durably persist regardless of what happens to other
        # gaps in the same request — one gap's HTTP timeout must not roll
        # back another gap's already-successful collection. This also keeps
        # the race-dedup rollback in create_task (see job_repository) scoped
        # to only that gap's own uncommitted insert attempt.
        results = []
        for gap in gaps:
            result = await self._process_gap(job_id, gap)
            await self._session.commit()
            results.append(result)

        has_failed_task = any(r.status == JobTaskStatus.FAILED for r in results)
        await self._job_repo.finalize_job(job, has_failed_task=has_failed_task)
        await self._session.commit()
        return job_id, results

    async def _process_gap(self, job_id: uuid.UUID, gap: CoverageGap) -> JobTaskResult:
        existing = await self._coverage_repo.get(
            gap.source.value, gap.entity_key, gap.country, gap.indicator_code, gap.period_key
        )
        if not gap.force and self._coverage_repo.is_fresh(existing):
            task = await self._job_repo.mark_skipped(
                job_id=job_id,
                source=gap.source.value,
                entity_key=gap.entity_key,
                country=gap.country,
                indicator_code=gap.indicator_code,
                period_key=gap.period_key,
            )
            return job_task_to_result(task)

        active = await self._job_repo.find_active_task(
            source=gap.source.value,
            entity_key=gap.entity_key,
            country=gap.country,
            indicator_code=gap.indicator_code,
            period_key=gap.period_key,
        )
        if active is not None:
            logger.info(
                f"Gap already in flight, attaching to task {active.id}: "
                f"{gap.source.value}/{gap.country}/{gap.indicator_code}"
            )
            return job_task_to_result(active)

        claimed_task = await self._job_repo.create_task(
            job_id=job_id,
            source=gap.source.value,
            entity_key=gap.entity_key,
            country=gap.country,
            indicator_code=gap.indicator_code,
            period_key=gap.period_key,
        )
        if claimed_task is None:
            # Lost the race: a concurrent request committed an equivalent
            # task between our SELECT and our INSERT.
            attached = await self._job_repo.find_active_task(
                source=gap.source.value,
                entity_key=gap.entity_key,
                country=gap.country,
                indicator_code=gap.indicator_code,
                period_key=gap.period_key,
            )
            if attached is not None:
                return job_task_to_result(attached)
            # It must have finished in that same window — treat as covered.
            skipped = await self._job_repo.mark_skipped(
                job_id=job_id,
                source=gap.source.value,
                entity_key=gap.entity_key,
                country=gap.country,
                indicator_code=gap.indicator_code,
                period_key=gap.period_key,
            )
            return job_task_to_result(skipped)

        return await self._dispatch_and_poll(claimed_task, gap)

    async def _dispatch_and_poll(self, task: JobTask, gap: CoverageGap) -> JobTaskResult:
        # Country-less indicators (the worldwide PyTrends interest-by-country
        # breakdown) use the sentinel ISO3 "WLD" and carry no real country —
        # the collection request is built without a country code, so skip the
        # canonical-countries lookup instead of failing on "WLD not found".
        if gap.indicator_code in _COUNTRYLESS_INDICATORS:
            country = None
        else:
            country = await self._country_repo.get_by_iso3(gap.country)
            if country is None:
                await self._job_repo.mark_failed(
                    task, error_message=f"Unknown country {gap.country} (not in canonical countries table)"
                )
                return job_task_to_result(task)

        try:
            ingestion_run_id = await self._collection_client.dispatch(
                CollectionRequest(
                    source=gap.source,
                    indicator_code=gap.indicator_code,
                    country=country,
                    collection_params=gap.collection_params,
                )
            )
            await self._job_repo.mark_running(task, ingestion_run_id=ingestion_run_id)
            status = await self._collection_client.poll_until_done(gap.source, ingestion_run_id)
        except MarketMindCoreError as exc:
            await self._job_repo.mark_failed(task, error_message=str(exc))
            return job_task_to_result(task)

        if status != IngestionRunStatus.SUCCESS:
            await self._job_repo.mark_failed(
                task, error_message=f"{gap.source.value} ingestion run ended with status={status.value}"
            )
            return job_task_to_result(task)

        await self._coverage_repo.upsert_collected(
            source=gap.source.value,
            entity_key=gap.entity_key,
            country=gap.country,
            indicator_code=gap.indicator_code,
            period_key=gap.period_key,
            is_closed=gap.is_closed,
            ttl_days=gap.ttl_days if gap.ttl_days is not None else settings.default_coverage_ttl_days,
        )
        await self._job_repo.mark_done(task)
        return job_task_to_result(task)


def job_task_to_result(task: JobTask) -> JobTaskResult:
    return JobTaskResult(
        id=task.id,
        source=DataSource(task.source),
        entity_key=task.entity_key,
        country=task.country,
        indicator_code=task.indicator_code,
        period_key=task.period_key,
        status=JobTaskStatus(task.status),
        ingestion_run_id=task.ingestion_run_id,
        error_message=task.error_message,
    )
