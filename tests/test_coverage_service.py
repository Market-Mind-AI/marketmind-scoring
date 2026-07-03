import uuid
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import DataSource, IngestionRunStatus, JobTaskStatus
from app.schemas.coverage import CoverageGap
from app.services.collection_client import CollectionDispatchError
from app.services.coverage_service import CoverageService


def _mock_client(
    *, run_id: uuid.UUID | None = None, status: IngestionRunStatus = IngestionRunStatus.SUCCESS
) -> AsyncMock:
    client = AsyncMock()
    client.dispatch.return_value = run_id or uuid.uuid4()
    client.poll_until_done.return_value = status
    return client


async def test_gap_already_fresh_is_skipped_without_dispatch(db_session: AsyncSession) -> None:
    client = _mock_client()
    service = CoverageService(db_session, collection_client=client)
    gap = CoverageGap(
        source=DataSource.WORLDBANK,
        country="USA",
        indicator_code=f"gdp-{uuid.uuid4()}",
        period_key="2026",
        is_closed=True,
        collection_params={"start_year": 2020, "end_year": 2026},
    )

    # First call collects it (nothing covered yet).
    job_id_1, results_1 = await service.check_or_collect([gap], requested_by="test")
    assert results_1[0].status == JobTaskStatus.DONE
    client.dispatch.assert_awaited_once()

    # Second call for the exact same (closed) gap must not dispatch again.
    client.dispatch.reset_mock()
    job_id_2, results_2 = await service.check_or_collect([gap], requested_by="test")
    assert job_id_2 != job_id_1
    assert results_2[0].status == JobTaskStatus.SKIPPED
    client.dispatch.assert_not_awaited()


async def test_successful_collection_updates_coverage(db_session: AsyncSession) -> None:
    run_id = uuid.uuid4()
    client = _mock_client(run_id=run_id, status=IngestionRunStatus.SUCCESS)
    service = CoverageService(db_session, collection_client=client)
    gap = CoverageGap(
        source=DataSource.OECD,
        country="ESP",
        indicator_code=f"cpi-{uuid.uuid4()}",
        period_key="2026-07",
        collection_params={"start_period": "2026-01", "end_period": "2026-07"},
    )

    job_id, results = await service.check_or_collect([gap], requested_by="test")
    assert results[0].status == JobTaskStatus.DONE
    assert results[0].ingestion_run_id == run_id

    coverage = await service.check_coverage([gap])
    # gap didn't set ttl_days explicitly -> settings.default_coverage_ttl_days (30d) applied on collection.
    assert coverage[0].covered is True


async def test_failed_ingestion_run_marks_task_failed(db_session: AsyncSession) -> None:
    client = _mock_client(status=IngestionRunStatus.FAILED)
    service = CoverageService(db_session, collection_client=client)
    gap = CoverageGap(
        source=DataSource.UNDATA,
        country="BRA",
        indicator_code=f"population-{uuid.uuid4()}",
        period_key="2026",
        collection_params={"start_year": 2020, "end_year": 2026},
    )

    job_id, results = await service.check_or_collect([gap], requested_by="test")
    assert results[0].status == JobTaskStatus.FAILED
    assert results[0].error_message is not None


async def test_dispatch_error_marks_task_failed_not_raised(db_session: AsyncSession) -> None:
    client = AsyncMock()
    client.dispatch.side_effect = CollectionDispatchError("boom")
    service = CoverageService(db_session, collection_client=client)
    gap = CoverageGap(
        source=DataSource.WORLDBANK,
        country="USA",
        indicator_code=f"gdp-{uuid.uuid4()}",
        period_key="2026",
        collection_params={"start_year": 2020, "end_year": 2026},
    )

    job_id, results = await service.check_or_collect([gap], requested_by="test")
    assert results[0].status == JobTaskStatus.FAILED
    assert "boom" in (results[0].error_message or "")


async def test_unknown_country_fails_cleanly(db_session: AsyncSession) -> None:
    client = _mock_client()
    service = CoverageService(db_session, collection_client=client)
    gap = CoverageGap(
        source=DataSource.WORLDBANK,
        country="ZZZ",
        indicator_code=f"gdp-{uuid.uuid4()}",
        period_key="2026",
        collection_params={"start_year": 2020, "end_year": 2026},
    )

    job_id, results = await service.check_or_collect([gap], requested_by="test")
    assert results[0].status == JobTaskStatus.FAILED
    client.dispatch.assert_not_awaited()


async def test_attaches_to_already_active_task_instead_of_redispatching(db_session: AsyncSession) -> None:
    """Simulates the steady-state race outcome directly (see
    test_job_repository.test_find_active_task_sees_committed_pending_task
    for the lower-level guarantee): a PENDING/RUNNING task for the exact
    same gap, created and committed by some other request, must be attached
    to rather than triggering a second collection call."""
    from app.repositories.job_repository import JobRepository

    job_repo = JobRepository(db_session)
    other_job_id = (await job_repo.create_job(requested_by="other-caller", request_payload={})).id
    indicator_code = f"gdp-{uuid.uuid4()}"
    other_task = await job_repo.create_task(
        job_id=other_job_id,
        source=DataSource.WORLDBANK.value,
        entity_key=None,
        country="USA",
        indicator_code=indicator_code,
        period_key="2026",
    )
    assert other_task is not None
    await db_session.commit()

    client = _mock_client()
    service = CoverageService(db_session, collection_client=client)
    gap = CoverageGap(
        source=DataSource.WORLDBANK,
        country="USA",
        indicator_code=indicator_code,
        period_key="2026",
        collection_params={"start_year": 2020, "end_year": 2026},
    )

    job_id, results = await service.check_or_collect([gap], requested_by="test")
    assert results[0].id == other_task.id
    assert results[0].status == JobTaskStatus.PENDING
    client.dispatch.assert_not_awaited()
