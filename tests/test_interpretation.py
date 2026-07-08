import pytest

from app.domain.enums import IndicatorCode, IndicatorDimension
from app.services.mos.interpretation import (
    DIMENSION_MEANING,
    INDICATOR_MEANING,
    mos_band,
    score_band,
)


class TestScoreBand:
    @pytest.mark.parametrize(
        ("score", "expected"),
        [
            (0.0, "Very Low"),
            (19.99, "Very Low"),
            (20.0, "Low"),
            (39.99, "Low"),
            (40.0, "Moderate"),
            (59.99, "Moderate"),
            (60.0, "High"),
            (79.99, "High"),
            (80.0, "Very High"),
            (100.0, "Very High"),
        ],
    )
    def test_boundaries(self, score: float, expected: str) -> None:
        assert score_band(score) == expected


class TestMosBand:
    @pytest.mark.parametrize(
        ("mos", "expected"),
        [
            (0.0, "Very Low Opportunity"),
            (19.99, "Very Low Opportunity"),
            (20.0, "Low Opportunity"),
            (39.99, "Low Opportunity"),
            (40.0, "Moderate Opportunity"),
            (59.99, "Moderate Opportunity"),
            (60.0, "Good Opportunity"),
            (74.99, "Good Opportunity"),
            (75.0, "High Opportunity"),
            (89.99, "High Opportunity"),
            (90.0, "Excellent Opportunity"),
            (100.0, "Excellent Opportunity"),
        ],
    )
    def test_boundaries(self, mos: float, expected: str) -> None:
        assert mos_band(mos) == expected


class TestMeaningCompleteness:
    def test_every_indicator_code_has_a_meaning(self) -> None:
        missing = [code.value for code in IndicatorCode if code not in INDICATOR_MEANING]
        assert missing == []

    def test_every_dimension_has_a_meaning(self) -> None:
        missing = [dim.value for dim in IndicatorDimension if dim not in DIMENSION_MEANING]
        assert missing == []
