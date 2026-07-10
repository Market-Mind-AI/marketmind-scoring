import uuid
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
    coverage_service = _install("coverage_service", "app.services.analysis_service.CoverageService")
    indicator_engine = _install("indicator_engine", "app.services.analysis_service.IndicatorEngine")
    mos_engine = _install("mos_engine", "app.services.analysis_service.MosEngine")
    weight_calibration = _install("weight_calibration", "app.services.analysis_service.WeightCalibrationService")

    analysis_repo.get = AsyncMock(return_value=_analysis_request())
    analysis_repo.mark_running = AsyncMock()
    analysis_repo.mark_done = AsyncMock()
    analysis_repo.mark_failed = AsyncMock()

    product_repo.get_by_entity_key = AsyncMock(return_value=SimpleNamespace(canonical_title="air fryer"))

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
