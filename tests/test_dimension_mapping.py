import uuid
from datetime import datetime
from decimal import Decimal

from app.db.models.indicator_value import IndicatorValue
from app.domain.enums import IndicatorDimension
from app.services.mos.dimension_mapping import (
    MANDATORY_INDICATORS,
    check_availability,
    compute_dimension_subscores,
)

# Every mandatory indicator except inflation, at a fixed 50.0 normalized score.
_ALL_MANDATORY_CODES = [c.value for c in MANDATORY_INDICATORS]


def _iv(code: str, score: float | None, *, availability_status: str = "available") -> IndicatorValue:
    return IndicatorValue(
        id=uuid.uuid4(),
        entity_key=None,
        country="USA",
        indicator_code=code,
        period_type="month",
        period_key="2026-07",
        source="worldbank",
        raw_value=Decimal("1"),
        normalized_score=None if score is None else Decimal(str(score)),
        direction="direct",
        calculation_window="test",
        availability_status=availability_status,
        notes=None,
        computed_at=datetime(2026, 7, 2),
    )


def _full_values(*, oecd_inflation: bool = True) -> dict[str, IndicatorValue]:
    values = {code: _iv(code, 50.0) for code in _ALL_MANDATORY_CODES}
    if oecd_inflation:
        values["oecd_inflation_level_score"] = _iv("oecd_inflation_level_score", 50.0)
        values["oecd_inflation_volatility_score"] = _iv("oecd_inflation_volatility_score", 50.0)
    else:
        values["worldbank_inflation_score"] = _iv("worldbank_inflation_score", 50.0)
    return values


class TestCheckAvailability:
    def test_full_set_with_oecd_inflation_is_eligible(self) -> None:
        assert check_availability(_full_values(oecd_inflation=True)) is None

    def test_full_set_with_worldbank_inflation_fallback_is_eligible(self) -> None:
        assert check_availability(_full_values(oecd_inflation=False)) is None

    def test_missing_mandatory_indicator_is_excluded(self) -> None:
        values = _full_values()
        del values["gdp_per_capita_score"]
        reason = check_availability(values)
        assert reason is not None
        assert "gdp_per_capita_score" in reason

    def test_missing_both_inflation_sources_is_excluded(self) -> None:
        values = _full_values(oecd_inflation=True)
        del values["oecd_inflation_level_score"]
        del values["oecd_inflation_volatility_score"]
        reason = check_availability(values)
        assert reason is not None
        assert "inflation" in reason

    def test_median_age_fit_absence_does_not_exclude(self) -> None:
        values = _full_values()
        assert "median_age_fit_score" not in values
        assert check_availability(values) is None

    def test_unavailable_status_counts_as_missing_even_if_row_present(self) -> None:
        values = _full_values()
        values["gdp_per_capita_score"] = _iv("gdp_per_capita_score", None, availability_status="unavailable")
        reason = check_availability(values)
        assert reason is not None
        assert "gdp_per_capita_score" in reason


class TestComputeDimensionSubscores:
    def test_averages_scores_within_a_dimension(self) -> None:
        values = {
            "trend_score": _iv("trend_score", 80.0),
            "growth_score": _iv("growth_score", 60.0),
        }
        subscores = compute_dimension_subscores(values)
        assert subscores[IndicatorDimension.DEMAND] == 70.0

    def test_median_age_fit_contributes_only_when_present(self) -> None:
        without_maf = compute_dimension_subscores({"population_growth_score": _iv("population_growth_score", 50.0)})
        with_maf = compute_dimension_subscores(
            {
                "population_growth_score": _iv("population_growth_score", 50.0),
                "median_age_fit_score": _iv("median_age_fit_score", 100.0),
            }
        )
        assert without_maf[IndicatorDimension.DEMOGRAPHIC_SUSTAINABILITY] == 50.0
        assert with_maf[IndicatorDimension.DEMOGRAPHIC_SUSTAINABILITY] == 75.0

    def test_dimension_absent_entirely_when_no_indicator_available(self) -> None:
        subscores = compute_dimension_subscores({})
        assert subscores == {}

    def test_indicator_with_none_score_is_skipped(self) -> None:
        values = {"trend_score": _iv("trend_score", None, availability_status="unavailable")}
        assert compute_dimension_subscores(values) == {}
