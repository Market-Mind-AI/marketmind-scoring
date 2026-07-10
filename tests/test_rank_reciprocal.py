import pytest

from app.domain.enums import IndicatorDimension
from app.services.mos.rank_reciprocal import PRIORITY_ORDER, rank_reciprocal_weights


class TestRankReciprocalWeights:
    def test_weights_sum_to_one(self) -> None:
        weights = rank_reciprocal_weights(PRIORITY_ORDER)
        assert sum(weights.values()) == pytest.approx(1.0)

    def test_first_ranked_gets_the_largest_weight(self) -> None:
        weights = rank_reciprocal_weights(PRIORITY_ORDER)
        ordered = [weights[d] for d in PRIORITY_ORDER]
        assert ordered == sorted(ordered, reverse=True)

    def test_matches_known_six_item_rank_reciprocal_values(self) -> None:
        # w_j = (1/j) / H_n, H_6 = 1 + 1/2 + 1/3 + 1/4 + 1/5 + 1/6 = 2.45
        weights = rank_reciprocal_weights(PRIORITY_ORDER)
        expected = [0.40816, 0.20408, 0.13605, 0.10204, 0.08163, 0.06803]
        for dimension, value in zip(PRIORITY_ORDER, expected, strict=True):
            assert weights[dimension] == pytest.approx(value, abs=1e-4)

    def test_demand_is_ranked_first(self) -> None:
        weights = rank_reciprocal_weights(PRIORITY_ORDER)
        assert max(weights, key=lambda d: weights[d]) == IndicatorDimension.DEMAND

    def test_market_size_is_ranked_last(self) -> None:
        # User decision 2026-07-10: Market Size (raw population/urbanization)
        # is the most "generic big country" signal and moved to last to cut
        # the generic-size bias — see marketmind_mos_engine.
        weights = rank_reciprocal_weights(PRIORITY_ORDER)
        assert min(weights, key=lambda d: weights[d]) == IndicatorDimension.MARKET_SIZE

    def test_macro_demand_structure_ranked_second(self) -> None:
        assert PRIORITY_ORDER[1] == IndicatorDimension.MACRO_DEMAND_STRUCTURE

    def test_generic_wealth_size_ranked_below_product_fit(self) -> None:
        # User decision 2026-07-10: Economic Readiness (GDP per capita) and
        # Market Size (population) were pushed below the product-fit signals
        # (Demand, Macro Demand Structure, Digital Readiness).
        for generic in (IndicatorDimension.ECONOMIC_READINESS, IndicatorDimension.MARKET_SIZE):
            assert PRIORITY_ORDER.index(generic) > PRIORITY_ORDER.index(IndicatorDimension.DIGITAL_READINESS)

    def test_priority_order_has_all_six_dimensions(self) -> None:
        assert set(PRIORITY_ORDER) == set(IndicatorDimension)

    def test_single_item_order_gets_full_weight(self) -> None:
        weights = rank_reciprocal_weights([IndicatorDimension.DEMAND])
        assert weights == {IndicatorDimension.DEMAND: pytest.approx(1.0)}
