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

    analysis_repo.get = AsyncMock(return_value=_analysis_request())
    analysis_repo.mark_running = AsyncMock()
    analysis_repo.mark_done = AsyncMock()
    analysis_repo.mark_failed = AsyncMock()

    product_repo.get_by_entity_key = AsyncMock(
        return_value=SimpleNamespace(canonical_title="air fryer", localized_terms={"en": "air fryer"})
    )
    country_repo.get_by_iso3 = AsyncMock(
        return_value=SimpleNamespace(search_language="en", pytrends_geo=None, iso2="US")
    )
    best_term_patcher = patch(
        "app.services.analysis_service.pytrends_indicators.best_localized_term",
        new_callable=AsyncMock,
        side_effect=lambda base, country_code, lang, fallback: fallback,
    )
    best_term_patcher.start()
    mocks["_patcher_best_term"] = best_term_patcher

    # The keyword-aware guard checks the per-country series exists for the
    # winning term; default to present so it doesn't force extra collection.
    series_patcher = patch(
        "app.services.analysis_service.pytrends_indicators.has_per_country_series",
        new_callable=AsyncMock,
        return_value=True,
    )
    mocks["has_per_country_series"] = series_patcher.start()
    mocks["_patcher_has_series"] = series_patcher

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


async def test_forces_per_country_collection_when_winning_term_series_absent() -> None:
    # Keyword-aware guard (2026-07-14): even when coverage_index says the
    # per-country PyTrends indicators are fresh (all_covered=True), a shift in
    # the winning localized term leaves interest_over_time holding only the OLD
    # term's series. has_per_country_series returns False for the current
    # winner, so the per-country PyTrends gaps are force-collected anyway.
    session, mocks = _patched_service(all_covered=True)
    mocks["has_per_country_series"].return_value = False
    try:
        service = AnalysisService(session)
        with patch("app.services.analysis_service.current_month_period_key", return_value="2026-07"):
            await service.run(_REQUEST_ID)
    finally:
        for key, value in list(mocks.items()):
            if key.startswith("_patcher_"):
                value.stop()

    mocks["coverage_service"].check_or_collect.assert_awaited()
    dispatched = mocks["coverage_service"].check_or_collect.call_args.args[0]
    pytrends_gaps = [g for g in dispatched if g.source == DataSource.PYTRENDS and g.entity_key is not None]
    assert len(pytrends_gaps) == 3  # interest_over_time, interest_by_region, related_queries
    assert all(g.force for g in pytrends_gaps)  # forced past the fresh coverage row


async def test_worldwide_demand_gap_sends_all_variations_per_language() -> None:
    # Data arbitration (2026-07-11): the worldwide interest_by_country
    # collection compares ALL local-name variations for each distinct search
    # language among the requested countries, so real search volume can pick
    # the genuine local name and each country SUMS its language's variations.
    session, mocks = _patched_service(all_covered=False)
    mocks["analysis_repo"].get = AsyncMock(return_value=_analysis_request(countries=["ESP", "PRT", "MEX"]))
    mocks["product_repo"].get_by_entity_key = AsyncMock(
        return_value=SimpleNamespace(
            canonical_title="jamon iberico",
            localized_terms={
                "es": ["jamón ibérico", "jamón pata negra"],
                "pt": ["presunto de porco preto", "presunto pata negra"],
            },
        )
    )
    language_by_iso3 = {"ESP": "es", "PRT": "pt", "MEX": "es"}
    iso2_by_iso3 = {"ESP": "ES", "PRT": "PT", "MEX": "MX"}
    mocks["country_repo"].get_by_iso3 = AsyncMock(
        side_effect=lambda iso3: SimpleNamespace(
            search_language=language_by_iso3[iso3], pytrends_geo=None, iso2=iso2_by_iso3[iso3]
        )
    )
    try:
        service = AnalysisService(session)
        await service.run(_REQUEST_ID)
    finally:
        for key, value in list(mocks.items()):
            if key.startswith("_patcher_"):
                value.stop()

    worldwide_gaps = [
        gap
        for call in mocks["coverage_service"].check_or_collect.await_args_list
        for gap in call.args[0]
        if gap.indicator_code == "interest_by_country"
    ]
    assert len(worldwide_gaps) == 1
    params = worldwide_gaps[0].collection_params
    assert params["base_keyword"] == "jamon iberico"
    terms_by_language: dict[str, list[str]] = {}
    for t in params["terms"]:
        terms_by_language.setdefault(t["language"], []).append(t["term"])
    # es and pt only (MEX shares es -> deduped), ALL variations per language
    assert terms_by_language == {
        "es": ["jamón ibérico", "jamón pata negra"],
        "pt": ["presunto de porco preto", "presunto pata negra"],
    }


async def test_run_aborts_when_data_incomplete_never_partial_mos() -> None:
    # Completeness gate: if any requested country is missing a mandatory
    # indicator after collection, the whole analysis fails — no partial MOS.
    session, mocks = _patched_service(all_covered=True)
    mocks["mos_engine"].find_incomplete_countries = AsyncMock(
        return_value={"USA": "Missing mandatory indicator(s): regional_demand_score"}
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
    mocks["analysis_repo"].mark_done.assert_awaited_once_with(mocks["analysis_repo"].get.return_value, result=[])
