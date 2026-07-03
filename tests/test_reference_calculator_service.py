import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.country import Country
from app.domain.enums import AvailabilityStatus, IndicatorDirection
from app.repositories.indicator_repository import IndicatorRepository
from app.services.indicators.results import RawIndicatorResult
from app.services.reference_calculator_service import ReferenceCalculatorService, _percentile


def _unique_period_key() -> str:
    # See the identical helper in test_indicator_engine.py: unique per test
    # so reruns against a persistent (not recreated) dev DB never collide
    # with a previous run's committed normalization_reference rows.
    return f"2026-07-test-{uuid.uuid4().hex[:8]}"


class TestPercentile:
    def test_median_of_odd_list(self) -> None:
        assert _percentile([1.0, 2.0, 3.0], 50) == 2.0

    def test_p0_and_p100_are_min_and_max(self) -> None:
        values = [10.0, 20.0, 30.0, 40.0]
        assert _percentile(values, 0) == 10.0
        assert _percentile(values, 100) == 40.0

    def test_single_value_returns_itself_at_any_percentile(self) -> None:
        assert _percentile([42.0], 5) == 42.0
        assert _percentile([42.0], 95) == 42.0

    def test_interpolates_between_ranks(self) -> None:
        # 5 values [0,10,20,30,40], p25 -> rank=1.0 -> exactly index 1 -> 10.0
        assert _percentile([0.0, 10.0, 20.0, 30.0, 40.0], 25) == 10.0


async def test_refresh_computes_reference_bounds_from_all_seeded_countries(db_session: AsyncSession) -> None:
    countries_result = await db_session.execute(select(Country))
    countries = list(countries_result.scalars().all())
    assert len(countries) >= 2
    value_by_iso3 = {c.iso3: float(i) for i, c in enumerate(countries)}

    def fake_compute(indicator_code: object, country: Country, iso2: str) -> RawIndicatorResult:
        return RawIndicatorResult(
            value_by_iso3[country.iso3], IndicatorDirection.DIRECT, "test", AvailabilityStatus.AVAILABLE, None
        )

    period_key = _unique_period_key()
    with patch("app.services.reference_calculator_service._compute_single", side_effect=fake_compute):
        service = ReferenceCalculatorService(db_session)
        report = await service.refresh(period_key=period_key)

    countries_with_iso2 = [c for c in countries if c.iso2 is not None]
    assert report["gdp_per_capita_score"] == len(countries_with_iso2)

    reference = await IndicatorRepository(db_session).get_reference("gdp_per_capita_score", period_key)
    assert reference is not None
    all_values = sorted(value_by_iso3[c.iso3] for c in countries_with_iso2)
    assert float(reference.min_x) == pytest.approx(_percentile(all_values, 5))
    assert float(reference.max_x) == pytest.approx(_percentile(all_values, 95))
    assert reference.is_closed is False


async def test_refresh_never_overwrites_a_closed_period(db_session: AsyncSession) -> None:
    period_key = _unique_period_key()
    indicator_repo = IndicatorRepository(db_session)
    await indicator_repo.upsert_reference(
        indicator_code="gdp_per_capita_score", period_key=period_key, min_x=1, max_x=2,
        reference_population_size=27, is_closed=True,
    )  # fmt: skip
    await db_session.commit()

    def fake_compute(indicator_code: object, country: Country, iso2: str) -> RawIndicatorResult:
        return RawIndicatorResult(999.0, IndicatorDirection.DIRECT, "test", AvailabilityStatus.AVAILABLE, None)

    with patch("app.services.reference_calculator_service._compute_single", side_effect=fake_compute):
        service = ReferenceCalculatorService(db_session)
        await service.refresh(period_key=period_key)

    reference = await indicator_repo.get_reference("gdp_per_capita_score", period_key)
    assert reference is not None
    assert float(reference.min_x) == 1  # unchanged
    assert float(reference.max_x) == 2  # unchanged
