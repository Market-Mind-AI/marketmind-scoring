from unittest.mock import patch

import pytest

from app.domain.enums import AvailabilityStatus, IndicatorDirection
from app.services.indicators import oecd as oecd_indicators
from tests._external_db_mock import make_mock_engine, result_with_all

# OECD inflation (CPI Level/Volatility) was removed as a source 2026-07-08 —
# World Bank is the sole inflation source now (see test_indicators_worldbank).
# Only Real Estate Growth remains OECD-derived.


async def test_real_estate_growth_compares_recent_vs_old_window() -> None:
    # DB returns DESC by time_period: most recent 4 quarters first, oldest
    # 4 quarters (within the ~3y window) last.
    recent_window = [110.0, 108.0, 107.0, 105.0]
    old_window = [95.0, 94.0, 93.0, 92.0]
    series = recent_window + [100.0, 99.0, 98.0, 97.0] + old_window
    engine = make_mock_engine([result_with_all([(v,) for v in series])])
    with patch("app.services.indicators.oecd.get_external_engine", return_value=engine):
        result = await oecd_indicators.compute_real_estate_growth("ESP")

    recent_avg = sum(recent_window) / 4
    old_avg = sum(old_window) / 4
    expected_growth = ((recent_avg - old_avg) / old_avg) * 100

    assert result.availability_status == AvailabilityStatus.CONDITIONAL
    assert result.direction == IndicatorDirection.DIRECT
    assert result.raw_value == pytest.approx(expected_growth)


async def test_real_estate_growth_conditional_when_fewer_than_eight_quarters() -> None:
    engine = make_mock_engine([result_with_all([(100.0,), (99.0,), (98.0,)])])
    with patch("app.services.indicators.oecd.get_external_engine", return_value=engine):
        result = await oecd_indicators.compute_real_estate_growth("BRA")

    assert result.availability_status == AvailabilityStatus.CONDITIONAL
    assert result.raw_value is None
