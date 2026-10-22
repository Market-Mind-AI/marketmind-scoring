import uuid
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import DataSource, IngestionRunStatus, JobTaskStatus
from app.schemas.coverage import CoverageGap
from app.services.collection_client import CollectionDispatchError
from app.services.coverage_service import CoverageService


def _mock_client(
    *,
    run_id: uuid.UUID | None = None,
    status: IngestionRunStatus = IngestionRunStatus.SUCCESS,
    fresh: bool = False,
) -> AsyncMock:
    client = AsyncMock()
    client.dispatch.return_value = run_id or uuid.uuid4()
    client.poll_until_done.return_value = status
    # Freshness is answered by the service that owns the data. Spelled out
    # rather than left to AsyncMock's default, which returns a truthy object
    # for every lookup and would make every gap look stale by accident.
    client.freshness.side_effect = lambda source, *, countries, indicators, stale_after_days: {
        (c, i): not fresh for c in countries for i in indicators
    }
    return client


async def test_a_gap_the_owning_service_calls_fresh_is_not_collected_again(
    db_session: AsyncSession,
) -> None:
    """The decision belongs to whoever holds the data. This service used to keep
    its own record of what had been collected and decide from that — a second
    copy of the same fact, and when the two disagreed the local one won."""
    client = _mock_client(fresh=True)
    service = CoverageService(db_session, collection_client=client)
    gap = CoverageGap(
        source=DataSource.WORLDBANK,
        country="USA",
        indicator_code=f"gdp-{uuid.uuid4()}",
        period_key="2026",
        is_closed=True,
        collection_params={"start_year": 2020, "end_year": 2026},
    )

    _job_id, results = await service.check_or_collect([gap], requested_by="test")

    assert results[0].status == JobTaskStatus.SKIPPED
    client.dispatch.assert_not_awaited()


async def test_the_freshness_of_a_batch_is_asked_once_per_source(db_session: AsyncSession) -> None:
    """Not once per gap. Twenty-two indicators across two sources is two
    questions, and the endpoint takes every pair at once."""
    client = _mock_client(fresh=True)
    service = CoverageService(db_session, collection_client=client)
    gaps = [
        CoverageGap(
            source=source,
            country=country,
            indicator_code=f"i{n}",
            period_key="2026",
            collection_params={"start_year": 2020, "end_year": 2026},
        )
        for source in (DataSource.WORLDBANK, DataSource.OECD)
        for country in ("USA", "BRA")
        for n in range(5)
    ]

    await service.check_coverage(gaps)

    assert client.freshness.await_count == 2


async def test_a_demand_gap_is_never_skipped_as_fresh(db_session: AsyncSession) -> None:
    """PyTrends is the exception on purpose: demand is per product and per
    keyword, so there is nothing pre-stored to be fresh about. And a demand gap
    only exists because that service just said it lacks those terms — so
    skipping it would be believing a second record over the one that answered.
    That is exactly what happened while coverage_index was consulted here."""
    client = _mock_client(fresh=True)  # even told everything is fresh
    service = CoverageService(db_session, collection_client=client)
    gap = CoverageGap(
        source=DataSource.PYTRENDS,
        country="BRA",
        indicator_code="search_volume",
        period_key="2026-09",
        collection_params={"location": "BR", "keywords": ["picanha"], "language": "pt"},
    )

    _job_id, results = await service.check_or_collect([gap], requested_by="test")

    assert results[0].status != JobTaskStatus.SKIPPED
    client.dispatch.assert_awaited_once()
    client.freshness.assert_not_awaited(), "nothing to ask: the caller already asked"


async def test_a_collection_leaves_no_freshness_record_behind_here(db_session: AsyncSession) -> None:
    """It used to write one. The service that collected stamped its own
    last_checked_at as well, so the same fact lived in two places — one fact,
    one place now."""
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

    _job_id, results = await service.check_or_collect([gap], requested_by="test")
    assert results[0].status == JobTaskStatus.DONE
    assert results[0].ingestion_run_id == run_id

    # Asked again right after, and the answer comes from the source — which in
    # this test still reports stale, so it would collect again. The point is
    # that nothing local claims otherwise.
    coverage = await service.check_coverage([gap])
    assert coverage[0].covered is False
    assert coverage[0].last_fetched_at is None


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


async def test_the_un_service_is_asked_in_the_code_it_actually_indexes_by(db_session: AsyncSession) -> None:
    """Asking in the wrong code does not raise — it returns ZERO pairs, which
    the rule above reads as "never checked" and therefore stale. The UN service
    indexes by ISO2, and asking it for ISO3 made its ENTIRE collection redo
    itself on every pass, for ever: expensive, silent, and the exact opposite of
    what a freshness index is for."""
    from sqlalchemy import select

    from app.db.models.country import Country

    # O banco de teste já vem com os países semeados; só garante o que importa.
    pais = (await db_session.execute(select(Country).where(Country.iso3 == "BRA"))).scalar_one_or_none()
    if pais is None:
        pais = Country(iso3="BRA", iso2="BR", name="Brazil", continent="South America")
        db_session.add(pais)
        await db_session.flush()
    assert pais.iso2 == "BR"

    client = _mock_client(fresh=True)
    service = CoverageService(db_session, collection_client=client)
    gap = CoverageGap(
        source=DataSource.UNDATA,
        country="BRA",
        indicator_code="MEDIAN_AGE",
        period_key="2026",
        collection_params={"start_year": 2020, "end_year": 2026},
    )

    resultado = await service.check_coverage([gap])

    assert client.freshness.await_args.kwargs["countries"] == ["BR"], "perguntado em ISO2"
    assert resultado[0].covered is True, "e a resposta tem de casar com o gap, que é ISO3"


async def test_the_other_services_keep_being_asked_in_iso3(db_session: AsyncSession) -> None:
    client = _mock_client(fresh=True)
    service = CoverageService(db_session, collection_client=client)
    gap = CoverageGap(
        source=DataSource.WORLDBANK,
        country="BRA",
        indicator_code="NY.GDP.PCAP.CD",
        period_key="2026",
        collection_params={"start_year": 2020, "end_year": 2026},
    )

    await service.check_coverage([gap])

    assert client.freshness.await_args.kwargs["countries"] == ["BRA"]


async def test_a_country_indicator_is_asked_with_its_own_window(db_session: AsyncSession) -> None:
    """Yearly figures stay good for 90 days. Asked with the general 30-day
    window, a figure that had not changed was downloaded again every month
    (22/10/2026: 52 UN Data and 13 OECD pairs would have been re-fetched)."""
    client = _mock_client(fresh=True)
    service = CoverageService(db_session, collection_client=client)
    gaps = [
        CoverageGap(
            source=DataSource.WORLDBANK,
            country="USA",
            indicator_code="NY.GDP.PCAP.CD",
            period_key="FIXED",
            ttl_days=90,
            collection_params={"start_year": 2016, "end_year": 2026},
        ),
        CoverageGap(
            source=DataSource.WORLDBANK,
            country="USA",
            indicator_code="SP.POP.TOTL",
            period_key="FIXED",
            collection_params={"start_year": 2016, "end_year": 2026},
        ),
    ]

    await service.check_coverage(gaps)

    windows = sorted(call.kwargs["stale_after_days"] for call in client.freshness.call_args_list)
    assert windows == [30, 90], "a gap with its own window keeps it; one without uses the default"
