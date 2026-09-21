
import pytest

from app.domain.enums import AvailabilityStatus
from app.services.indicators import undata as undata_indicators
from tests._source_mock import patch_undata


async def test_population_growth_averages_last_three_years() -> None:
    with patch_undata([("2024", -0.064), ("2023", -0.05), ("2022", -0.03)]):
        result = await undata_indicators.compute_population_growth("ES")

    assert result.availability_status == AvailabilityStatus.AVAILABLE
    assert result.raw_value == pytest.approx((-0.064 - 0.05 - 0.03) / 3)


async def test_net_migration_rate_matches_years_with_both_series() -> None:
    with patch_undata([("2025", 96630.0), ("2024", 80000.0)], [("2025", 47889958.0), ("2024", 47800000.0)]):
        result = await undata_indicators.compute_net_migration_rate("ES")

    assert result.availability_status == AvailabilityStatus.AVAILABLE
    rate_2025 = (96630.0 / 47889958.0) * 1000
    rate_2024 = (80000.0 / 47800000.0) * 1000
    assert result.raw_value == pytest.approx((rate_2025 + rate_2024) / 2)


async def test_net_migration_rate_unavailable_when_no_overlapping_years() -> None:
    with patch_undata([]):
        result = await undata_indicators.compute_net_migration_rate("ES")

    assert result.availability_status == AvailabilityStatus.UNAVAILABLE


async def test_median_age_fit_conditional_without_target_age() -> None:
    result = await undata_indicators.compute_median_age_fit("ES", None, None)
    assert result.availability_status == AvailabilityStatus.CONDITIONAL
    assert result.raw_value is None


async def test_median_age_fit_computes_distance_from_center() -> None:
    with patch_undata([("2024", 45.85)]):
        result = await undata_indicators.compute_median_age_fit("ES", 30, 50)

    # center=40, half_width=10, D=|45.85-40|=5.85 -> (1 - 5.85/10) * 100
    assert result.availability_status == AvailabilityStatus.CONDITIONAL
    assert result.raw_value == pytest.approx((1 - 5.85 / 10) * 100)


async def test_median_age_fit_unavailable_when_no_data() -> None:
    with patch_undata([]):
        result = await undata_indicators.compute_median_age_fit("ES", 30, 50)

    assert result.availability_status == AvailabilityStatus.UNAVAILABLE
