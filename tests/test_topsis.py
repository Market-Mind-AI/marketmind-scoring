import pytest

from app.domain.enums import IndicatorDimension
from app.services.mos.topsis import topsis_score, trend_score_cap


class TestTopsisScore:
    def test_all_dimensions_at_ideal_scores_one(self) -> None:
        scores = dict.fromkeys(IndicatorDimension, 100.0)
        weights = dict.fromkeys(IndicatorDimension, 1 / 5)
        assert topsis_score(scores, weights) == pytest.approx(1.0)

    def test_all_dimensions_at_anti_ideal_scores_zero(self) -> None:
        scores = dict.fromkeys(IndicatorDimension, 0.0)
        weights = dict.fromkeys(IndicatorDimension, 1 / 5)
        assert topsis_score(scores, weights) == pytest.approx(0.0)

    def test_all_dimensions_at_midpoint_scores_half(self) -> None:
        scores = dict.fromkeys(IndicatorDimension, 50.0)
        weights = dict.fromkeys(IndicatorDimension, 1 / 5)
        assert topsis_score(scores, weights) == pytest.approx(0.5)

    def test_higher_weight_on_strong_dimension_increases_score(self) -> None:
        scores = {
            IndicatorDimension.DEMAND: 100.0,
            IndicatorDimension.DIGITAL_READINESS: 0.0,
            IndicatorDimension.ECONOMIC_READINESS: 50.0,
            IndicatorDimension.MARKET_SIZE: 50.0,
            IndicatorDimension.DEMOGRAPHIC_SUSTAINABILITY: 50.0,
        }
        equal_weights = dict.fromkeys(IndicatorDimension, 1 / 5)
        demand_heavy_weights = {
            IndicatorDimension.DEMAND: 0.6,
            IndicatorDimension.DIGITAL_READINESS: 0.1,
            IndicatorDimension.ECONOMIC_READINESS: 0.1,
            IndicatorDimension.MARKET_SIZE: 0.1,
            IndicatorDimension.DEMOGRAPHIC_SUSTAINABILITY: 0.1,
        }
        assert topsis_score(scores, demand_heavy_weights) > topsis_score(scores, equal_weights)


class TestTrendScoreCap:
    def test_below_20_caps_at_39(self) -> None:
        assert trend_score_cap(0.0) == 39.0
        assert trend_score_cap(19.99) == 39.0

    def test_between_20_and_40_caps_at_59(self) -> None:
        assert trend_score_cap(20.0) == 59.0
        assert trend_score_cap(39.99) == 59.0

    def test_40_and_above_has_no_cap(self) -> None:
        assert trend_score_cap(40.0) is None
        assert trend_score_cap(100.0) is None
