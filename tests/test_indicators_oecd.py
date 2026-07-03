import statistics
from unittest.mock import patch

import pytest

from app.domain.enums import AvailabilityStatus, IndicatorDirection
from app.services.indicators import oecd as oecd_indicators
from tests._external_db_mock import make_mock_engine, result_with_all


async def test_inflation_level_prefers_monthly_frequency() -> None:
    engine = make_mock_engine([result_with_all([(2.3,), (2.1,), (2.0,)])])
    with patch("app.services.indicators.oecd.get_external_engine", return_value=engine):
        result = await oecd_indicators.compute_oecd_inflation_level("ESP")

    assert result.availability_status == AvailabilityStatus.AVAILABLE
    assert result.direction == IndicatorDirection.INVERSE
    assert result.raw_value == pytest.approx((2.3 + 2.1 + 2.0) / 3)


async def test_inflation_level_falls_back_when_no_monthly_data() -> None:
    engine = make_mock_engine([result_with_all([]), result_with_all([(2.3,), (2.1,)])])
    with patch("app.services.indicators.oecd.get_external_engine", return_value=engine):
        result = await oecd_indicators.compute_oecd_inflation_level("ESP")

    assert result.availability_status == AvailabilityStatus.AVAILABLE
    assert result.raw_value == pytest.approx((2.3 + 2.1) / 2)


async def test_inflation_level_unavailable_when_no_data_at_all() -> None:
    engine = make_mock_engine([result_with_all([]), result_with_all([])])
    with patch("app.services.indicators.oecd.get_external_engine", return_value=engine):
        result = await oecd_indicators.compute_oecd_inflation_level("ESP")

    assert result.availability_status == AvailabilityStatus.UNAVAILABLE


async def test_inflation_volatility_computes_population_stddev() -> None:
    series = [2.3, 2.1, 2.0, 1.9]
    engine = make_mock_engine([result_with_all([(v,) for v in series])])
    with patch("app.services.indicators.oecd.get_external_engine", return_value=engine):
        result = await oecd_indicators.compute_oecd_inflation_volatility("ESP")

    assert result.availability_status == AvailabilityStatus.AVAILABLE
    assert result.raw_value == pytest.approx(statistics.pstdev(series))


async def test_inflation_volatility_unavailable_with_fewer_than_two_values() -> None:
    engine = make_mock_engine([result_with_all([(2.3,)])])
    with patch("app.services.indicators.oecd.get_external_engine", return_value=engine):
        result = await oecd_indicators.compute_oecd_inflation_volatility("ESP")

    assert result.availability_status == AvailabilityStatus.UNAVAILABLE
