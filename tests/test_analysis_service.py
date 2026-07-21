import uuid
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.domain.enums import DataSource
from app.schemas.coverage import GapStatus
from app.schemas.mos import MosResultResponse
from app.services.analysis_service import AnalysisService

_ENTITY_KEY = uuid.uuid4()
_REQUEST_ID = uuid.uuid4()


def _analysis_request(
    *, entity_key: uuid.UUID | None = _ENTITY_KEY, countries: list[str] | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        id=_REQUEST_ID,
        entity_key=entity_key,
        countries=countries or ["USA"],
        canonical_title="air fryer",
        status="pending",
    )


def _mos_result(country: str) -> MosResultResponse:
    return MosResultResponse(
        id=uuid.uuid4(),
        entity_key=_ENTITY_KEY,
        country=country,
        period_type="month",
        period_key="2026-07",
        mos=Decimal("70.0"),
        mos_before_cap=Decimal("70.0"),
        capped_by=None,
        topsis_score=Decimal("0.7"),
        library_topsis_score=None,
        library_mos=None,
        mos_interpretation="Good Opportunity",
        availability_status="available",
        excluded_reason=None,
        computed_at=datetime(2026, 7, 8),
    )


def _patched_service(*, all_covered: bool) -> tuple[AsyncMock, dict[str, AsyncMock]]:
    """Patches every collaborator AnalysisService constructs internally and
    returns (session, mocks) so tests can assert on call args."""
    mocks: dict[str, AsyncMock] = {}

    def _install(name: str, target: str) -> AsyncMock:
        patcher = patch(target)
        cls = patcher.start()
        mocks[f"_patcher_{name}"] = patcher  # keep alive for stop() in caller
        instance = cls.return_value
        mocks[name] = instance
        return instance

    analysis_repo = _install("analysis_repo", "app.services.analysis_service.AnalysisRepository")
    product_repo = _install("product_repo", "app.services.analysis_service.ProductRepository")
    country_repo = _install("country_repo", "app.services.analysis_service.CountryRepository")
    coverage_service = _install("coverage_service", "app.services.analysis_service.CoverageService")
    indicator_engine = _install("indicator_engine", "app.services.analysis_service.IndicatorEngine")
    mos_engine = _install("mos_engine", "app.services.analysis_service.MosEngine")
    weight_calibration = _install("weight_calibration", "app.services.analysis_service.WeightCalibrationService")
    explanation_client = _install("explanation_client", "app.services.analysis_service.ExplanationClient")
    explanation_client.generate = AsyncMock(return_value="MOS narrative (mock)")

    analysis_repo.get = AsyncMock(return_value=_analysis_request())
    analysis_repo.mark_running = AsyncMock()
    analysis_repo.mark_done = AsyncMock()
    analysis_repo.mark_failed = AsyncMock()

    # Demand is now collected from Google Ads keyword data (2026-07-15) with the
    # country's OWN approved terms in localized_markets[ISO3] — no worldwide
    # arbitration, no per-country-series guard, no pytrends_indicators calls here.
    product_repo.get_by_entity_key = AsyncMock(
        return_value=SimpleNamespace(
            canonical_title="air fryer",
            localized_markets={"USA": {"primary_name": "air fryer", "search_terms": ["air fryer", "airfryer"]}},
        )
    )
    country_repo.get_by_iso3 = AsyncMock(
        return_value=SimpleNamespace(search_language="en", pytrends_geo=None, iso2="US")
    )

    coverage_service.check_coverage = AsyncMock(
        side_effect=lambda gaps: [
            GapStatus(
                source=g.source,
                entity_key=g.entity_key,
                country=g.country,
                indicator_code=g.indicator_code,
                period_key=g.period_key,
                covered=all_covered,
            )  # fmt: skip
            for g in gaps
        ]
    )
    coverage_service.check_or_collect = AsyncMock(return_value=(uuid.uuid4(), []))

    indicator_engine.compute_all = AsyncMock(return_value=[])
    mos_engine.find_incomplete_countries = AsyncMock(return_value={})  # completeness gate: nothing missing
    mos_engine.compute_mos = AsyncMock(return_value=[_mos_result("USA")])
    weight_calibration.calibrate = AsyncMock(return_value={})

    session = AsyncMock()
    return session, mocks


async def test_run_happy_path_marks_done_with_result() -> None:
    session, mocks = _patched_service(all_covered=True)
    try:
        with patch("app.services.analysis_service.current_month_period_key", return_value="2026-07"):
            service = AnalysisService(session)
            await service.run(_REQUEST_ID)
    finally:
        for key, value in list(mocks.items()):
            if key.startswith("_patcher_"):
                value.stop()

    mocks["analysis_repo"].mark_running.assert_awaited_once()
    mocks["indicator_engine"].compute_all.assert_awaited_once_with(
        country_iso3="USA", entity_key=_ENTITY_KEY, period_key="2026-07"
    )
    mocks["mos_engine"].compute_mos.assert_awaited_once_with(
        entity_key=_ENTITY_KEY, countries=["USA"], period_key="2026-07"
    )
    mocks["coverage_service"].check_or_collect.assert_not_awaited()  # everything already covered
    mocks["analysis_repo"].mark_done.assert_awaited_once()
    result_arg = mocks["analysis_repo"].mark_done.call_args.kwargs["result"]
    assert result_arg[0]["country"] == "USA"
    assert result_arg[0]["mos_interpretation"] == "Good Opportunity"
    # Final LLM narrative generated (best-effort) and persisted with the result.
    mocks["explanation_client"].generate.assert_awaited_once()
    assert mocks["analysis_repo"].mark_done.call_args.kwargs["explanation"] == "MOS narrative (mock)"


async def test_run_dispatches_collection_only_for_missing_gaps() -> None:
    session, mocks = _patched_service(all_covered=False)
    try:
        service = AnalysisService(session)
        await service.run(_REQUEST_ID)
    finally:
        for key, value in list(mocks.items()):
            if key.startswith("_patcher_"):
                value.stop()

    mocks["coverage_service"].check_or_collect.assert_awaited()
    dispatched_gaps = mocks["coverage_service"].check_or_collect.call_args.args[0]
    assert len(dispatched_gaps) > 0


async def test_demand_gaps_built_from_localized_markets() -> None:
    # Demand (2026-07-15): each country's approved terms in localized_markets[ISO3]
    # produce exactly two Google Ads gaps — search_volume (level/growth/stability)
    # and keywords_for_keywords (intent) — with the country's ISO2 location and
    # search_language. No worldwide arbitration, no per-country-series guard.
    session, mocks = _patched_service(all_covered=False)
    try:
        service = AnalysisService(session)
        await service.run(_REQUEST_ID)
    finally:
        for key, value in list(mocks.items()):
            if key.startswith("_patcher_"):
                value.stop()

    dispatched = [
        gap
        for call in mocks["coverage_service"].check_or_collect.await_args_list
        for gap in call.args[0]
    ]
    pytrends_gaps = {g.indicator_code: g for g in dispatched if g.source == DataSource.PYTRENDS}
    assert set(pytrends_gaps) == {"search_volume", "keywords_for_keywords"}

    sv = pytrends_gaps["search_volume"]
    assert sv.entity_key == _ENTITY_KEY
    assert sv.collection_params == {"location": "US", "language": "en", "keywords": ["air fryer", "airfryer"]}

    kfk = pytrends_gaps["keywords_for_keywords"]
    assert kfk.collection_params == {"location": "US", "language": "en", "seed_keywords": ["air fryer", "airfryer"]}


async def test_run_aborts_when_data_incomplete_never_partial_mos() -> None:
    # Completeness gate: if any requested country is missing a mandatory
    # indicator after collection, the whole analysis fails — no partial MOS.
    session, mocks = _patched_service(all_covered=True)
    mocks["mos_engine"].find_incomplete_countries = AsyncMock(
        return_value={"USA": "Missing mandatory indicator(s): trend_score"}
    )
    try:
        service = AnalysisService(session)
        await service.run(_REQUEST_ID)
    finally:
        for key, value in list(mocks.items()):
            if key.startswith("_patcher_"):
                value.stop()

    mocks["mos_engine"].compute_mos.assert_not_awaited()  # never computed on partial data
    mocks["analysis_repo"].mark_failed.assert_awaited_once()
    assert "partial data" in mocks["analysis_repo"].mark_failed.call_args.kwargs["error_message"]
    mocks["analysis_repo"].mark_done.assert_not_awaited()


async def test_run_marks_failed_on_unexpected_exception() -> None:
    session, mocks = _patched_service(all_covered=True)
    mocks["indicator_engine"].compute_all = AsyncMock(side_effect=RuntimeError("boom"))
    try:
        service = AnalysisService(session)
        await service.run(_REQUEST_ID)
    finally:
        for key, value in list(mocks.items()):
            if key.startswith("_patcher_"):
                value.stop()

    mocks["analysis_repo"].mark_failed.assert_awaited_once()
    assert "boom" in mocks["analysis_repo"].mark_failed.call_args.kwargs["error_message"]
    mocks["analysis_repo"].mark_done.assert_not_awaited()


async def test_run_skips_mos_when_entity_key_is_none() -> None:
    session, mocks = _patched_service(all_covered=True)
    mocks["analysis_repo"].get = AsyncMock(return_value=_analysis_request(entity_key=None))
    try:
        service = AnalysisService(session)
        await service.run(_REQUEST_ID)
    finally:
        for key, value in list(mocks.items()):
            if key.startswith("_patcher_"):
                value.stop()

    mocks["mos_engine"].compute_mos.assert_not_awaited()
    mocks["analysis_repo"].mark_done.assert_awaited_once_with(
        mocks["analysis_repo"].get.return_value, result=[], explanation=None
    )
