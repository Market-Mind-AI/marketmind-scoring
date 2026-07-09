import uuid
from datetime import datetime
from decimal import Decimal

from app.db.models.indicator_value import IndicatorValue
from app.domain.enums import IndicatorCode, IndicatorDimension
from app.services.mos.dimension_mapping import (
    DIMENSION_BY_INDICATOR,
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


def _full_values() -> dict[str, IndicatorValue]:
    # worldbank_inflation_score is now a plain mandatory indicator (OECD path
    # removed 2026-07-08), so it's already in _ALL_MANDATORY_CODES.
    return {code: _iv(code, 50.0) for code in _ALL_MANDATORY_CODES}


class TestCheckAvailability:
    def test_full_set_is_eligible(self) -> None:
        assert check_availability(_full_values()) is None

    def test_missing_mandatory_indicator_is_excluded(self) -> None:
        values = _full_values()
        del values["gdp_per_capita_score"]
        reason = check_availability(values)
        assert reason is not None
        assert "gdp_per_capita_score" in reason

    def test_missing_inflation_is_excluded(self) -> None:
        values = _full_values()
        del values["worldbank_inflation_score"]
        reason = check_availability(values)
        assert reason is not None
        assert "worldbank_inflation_score" in reason

    def test_median_age_fit_absence_does_not_exclude(self) -> None:
        values = _full_values()
        assert "median_age_fit_score" not in values
        assert check_availability(values) is None

    def test_real_estate_growth_absence_does_not_exclude(self) -> None:
        values = _full_values()
        assert "real_estate_growth_score" not in values
        assert check_availability(values) is None

    def test_macro_demand_structure_indicators_are_mandatory(self) -> None:
        for code in (
            IndicatorCode.CONSUMER_SPENDING_SCORE,
            IndicatorCode.INVESTMENT_SCORE,
            IndicatorCode.NET_EXPORTS_SCORE,
            IndicatorCode.TRADE_OPENNESS_SCORE,
        ):
            assert code in MANDATORY_INDICATORS
            values = _full_values()
            del values[code.value]
            reason = check_availability(values)
            assert reason is not None
            assert code.value in reason

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

    def test_macro_demand_structure_averages_its_four_indicators(self) -> None:
        values = {
            "consumer_spending_score": _iv("consumer_spending_score", 80.0),
            "investment_score": _iv("investment_score", 60.0),
            "net_exports_score": _iv("net_exports_score", 40.0),
            "trade_openness_score": _iv("trade_openness_score", 20.0),
        }
        subscores = compute_dimension_subscores(values)
        assert subscores[IndicatorDimension.MACRO_DEMAND_STRUCTURE] == 50.0

    def test_real_estate_growth_contributes_to_economic_readiness_only_when_present(self) -> None:
        without = compute_dimension_subscores({"gdp_per_capita_score": _iv("gdp_per_capita_score", 50.0)})
        with_growth = compute_dimension_subscores(
            {
                "gdp_per_capita_score": _iv("gdp_per_capita_score", 50.0),
                "real_estate_growth_score": _iv("real_estate_growth_score", 100.0),
            }
        )
        assert without[IndicatorDimension.ECONOMIC_READINESS] == 50.0
        assert with_growth[IndicatorDimension.ECONOMIC_READINESS] == 75.0


class TestGovernmentExpenditureIsContextOnly:
    def test_government_expenditure_never_maps_to_a_dimension(self) -> None:
        assert IndicatorCode.GOVERNMENT_EXPENDITURE_SCORE not in DIMENSION_BY_INDICATOR

    def test_government_expenditure_never_mandatory(self) -> None:
        assert IndicatorCode.GOVERNMENT_EXPENDITURE_SCORE not in MANDATORY_INDICATORS

    def test_government_expenditure_ignored_even_if_a_score_is_present(self) -> None:
        # Defensive: even if a normalized_score ever got attached to this
        # code (it shouldn't, per NormalizationMethod.CONTEXT_ONLY), it must
        # not leak into any dimension sub-score.
        values = {
            "gdp_per_capita_score": _iv("gdp_per_capita_score", 50.0),
            "government_expenditure_score": _iv("government_expenditure_score", 0.0),
        }
        subscores = compute_dimension_subscores(values)
        assert subscores[IndicatorDimension.ECONOMIC_READINESS] == 50.0
