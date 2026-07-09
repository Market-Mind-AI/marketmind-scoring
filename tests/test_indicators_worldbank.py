import math
from unittest.mock import patch

import pytest

from app.domain.enums import AvailabilityStatus, IndicatorDirection
from app.services.indicators import worldbank as worldbank_indicators
from tests._external_db_mock import make_mock_engine, result_with_all, result_with_first


async def test_gdp_per_capita_uses_latest_value() -> None:
    engine = make_mock_engine([result_with_first((35326.77,))])
    with patch("app.services.indicators.worldbank.get_external_engine", return_value=engine):
        result = await worldbank_indicators.compute_gdp_per_capita("ES")

    assert result.availability_status == AvailabilityStatus.AVAILABLE
    assert result.raw_value == 35326.77
    assert result.direction == IndicatorDirection.DIRECT


async def test_gdp_per_capita_unavailable_when_missing() -> None:
    engine = make_mock_engine([result_with_first(None)])
    with patch("app.services.indicators.worldbank.get_external_engine", return_value=engine):
        result = await worldbank_indicators.compute_gdp_per_capita("ES")

    assert result.availability_status == AvailabilityStatus.UNAVAILABLE
    assert result.raw_value is None


async def test_internet_penetration_truncates_to_100() -> None:
    engine = make_mock_engine([result_with_first((105.0,))])
    with patch("app.services.indicators.worldbank.get_external_engine", return_value=engine):
        result = await worldbank_indicators.compute_internet_penetration("BR")

    assert result.raw_value == 100.0


async def test_mobile_connectivity_accepts_values_over_100() -> None:
    engine = make_mock_engine([result_with_first((101.93,))])
    with patch("app.services.indicators.worldbank.get_external_engine", return_value=engine):
        result = await worldbank_indicators.compute_mobile_connectivity("BR")

    assert result.raw_value == 101.93


async def test_gdp_growth_averages_last_three_years() -> None:
    engine = make_mock_engine([result_with_all([(3.2,), (2.5,), (1.8,)])])
    with patch("app.services.indicators.worldbank.get_external_engine", return_value=engine):
        result = await worldbank_indicators.compute_gdp_growth("BR")

    assert result.raw_value == pytest.approx((3.2 + 2.5 + 1.8) / 3)


async def test_population_scale_uses_natural_log() -> None:
    engine = make_mock_engine([result_with_first((211998573.0,))])
    with patch("app.services.indicators.worldbank.get_external_engine", return_value=engine):
        result = await worldbank_indicators.compute_population_scale("BR")

    assert result.raw_value == pytest.approx(math.log(211998573.0))


async def test_urbanization_truncates_to_100() -> None:
    engine = make_mock_engine([result_with_first((87.9,))])
    with patch("app.services.indicators.worldbank.get_external_engine", return_value=engine):
        result = await worldbank_indicators.compute_urbanization("BR")

    assert result.raw_value == 87.9


async def test_inflation_is_available_and_inverse() -> None:
    # World Bank is the sole inflation source now (OECD path removed
    # 2026-07-08) — availability is AVAILABLE like any other WB indicator,
    # no longer the special-case FALLBACK status.
    engine = make_mock_engine([result_with_all([(4.37,), (5.0,), (3.5,)])])
    with patch("app.services.indicators.worldbank.get_external_engine", return_value=engine):
        result = await worldbank_indicators.compute_inflation("BR")

    assert result.availability_status == AvailabilityStatus.AVAILABLE
    assert result.direction == IndicatorDirection.INVERSE
    assert result.raw_value == pytest.approx((4.37 + 5.0 + 3.5) / 3)


async def test_consumer_spending_uses_latest_value() -> None:
    engine = make_mock_engine([result_with_first((55.56,))])
    with patch("app.services.indicators.worldbank.get_external_engine", return_value=engine):
        result = await worldbank_indicators.compute_consumer_spending("ES")

    assert result.availability_status == AvailabilityStatus.AVAILABLE
    assert result.raw_value == 55.56
    assert result.direction == IndicatorDirection.DIRECT


async def test_consumer_spending_unavailable_when_missing() -> None:
    engine = make_mock_engine([result_with_first(None)])
    with patch("app.services.indicators.worldbank.get_external_engine", return_value=engine):
        result = await worldbank_indicators.compute_consumer_spending("ES")

    assert result.availability_status == AvailabilityStatus.UNAVAILABLE
    assert result.raw_value is None


async def test_government_expenditure_uses_latest_value() -> None:
    engine = make_mock_engine([result_with_first((19.20,))])
    with patch("app.services.indicators.worldbank.get_external_engine", return_value=engine):
        result = await worldbank_indicators.compute_government_expenditure("ES")

    assert result.availability_status == AvailabilityStatus.AVAILABLE
    assert result.raw_value == 19.20


async def test_investment_uses_latest_value() -> None:
    engine = make_mock_engine([result_with_first((20.61,))])
    with patch("app.services.indicators.worldbank.get_external_engine", return_value=engine):
        result = await worldbank_indicators.compute_investment("ES")

    assert result.availability_status == AvailabilityStatus.AVAILABLE
    assert result.raw_value == 20.61


async def test_net_exports_accepts_negative_values() -> None:
    engine = make_mock_engine([result_with_first((-2.5,))])
    with patch("app.services.indicators.worldbank.get_external_engine", return_value=engine):
        result = await worldbank_indicators.compute_net_exports("BR")

    assert result.availability_status == AvailabilityStatus.AVAILABLE
    assert result.raw_value == -2.5


async def test_trade_openness_uses_latest_value() -> None:
    engine = make_mock_engine([result_with_first((69.45,))])
    with patch("app.services.indicators.worldbank.get_external_engine", return_value=engine):
        result = await worldbank_indicators.compute_trade_openness("ES")

    assert result.availability_status == AvailabilityStatus.AVAILABLE
    assert result.raw_value == 69.45
