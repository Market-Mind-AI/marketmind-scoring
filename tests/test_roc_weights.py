import pytest

from app.domain.enums import IndicatorDimension
from app.services.mos.roc_weights import PRIORITY_ORDER, roc_weights


class TestRocWeights:
    def test_weights_sum_to_one(self) -> None:
        weights = roc_weights(PRIORITY_ORDER)
        assert sum(weights.values()) == pytest.approx(1.0)

    def test_first_ranked_gets_the_largest_weight(self) -> None:
        weights = roc_weights(PRIORITY_ORDER)
        ordered = [weights[d] for d in PRIORITY_ORDER]
        assert ordered == sorted(ordered, reverse=True)

    def test_matches_known_five_item_roc_values(self) -> None:
        # Standard ROC values for n=5 (Barron & Barrett, 1996):
        # 0.4567, 0.2567, 0.1567, 0.09, 0.04
        weights = roc_weights(PRIORITY_ORDER)
        expected = [0.4567, 0.2567, 0.1567, 0.09, 0.04]
        for dimension, value in zip(PRIORITY_ORDER, expected, strict=True):
            assert weights[dimension] == pytest.approx(value, abs=1e-4)

    def test_demand_is_ranked_first(self) -> None:
        weights = roc_weights(PRIORITY_ORDER)
        assert max(weights, key=lambda d: weights[d]) == IndicatorDimension.DEMAND

    def test_demographic_sustainability_is_ranked_last(self) -> None:
        weights = roc_weights(PRIORITY_ORDER)
        assert min(weights, key=lambda d: weights[d]) == IndicatorDimension.DEMOGRAPHIC_SUSTAINABILITY

    def test_single_item_order_gets_full_weight(self) -> None:
        weights = roc_weights([IndicatorDimension.DEMAND])
        assert weights == {IndicatorDimension.DEMAND: pytest.approx(1.0)}
