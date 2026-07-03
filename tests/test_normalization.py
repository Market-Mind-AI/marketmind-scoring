import uuid
from decimal import Decimal

from app.db.models.normalization_reference import NormalizationReference
from app.domain.enums import IndicatorCode, IndicatorDirection, NormalizationMethod
from app.services.indicators.normalization import normalization_method, normalize


def _reference(min_x: float, max_x: float) -> NormalizationReference:
    return NormalizationReference(
        id=uuid.uuid4(), indicator_code="x", period_key="2026-07", min_x=Decimal(str(min_x)), max_x=Decimal(str(max_x)),
        reference_population_size=10, is_closed=False,
    )  # fmt: skip


class TestNormalizationMethod:
    def test_passthrough_indicators(self) -> None:
        assert normalization_method(IndicatorCode.TREND_SCORE) == NormalizationMethod.PASSTHROUGH
        assert normalization_method(IndicatorCode.REGIONAL_DEMAND_SCORE) == NormalizationMethod.PASSTHROUGH
        assert normalization_method(IndicatorCode.SEARCH_INTENT_SCORE) == NormalizationMethod.PASSTHROUGH
        assert normalization_method(IndicatorCode.INTERNET_PENETRATION_SCORE) == NormalizationMethod.PASSTHROUGH
        assert normalization_method(IndicatorCode.URBANIZATION_SCORE) == NormalizationMethod.PASSTHROUGH
        assert normalization_method(IndicatorCode.MEDIAN_AGE_FIT_SCORE) == NormalizationMethod.PASSTHROUGH

    def test_fixed_bounds_indicators(self) -> None:
        assert normalization_method(IndicatorCode.GROWTH_SCORE) == NormalizationMethod.MINMAX_FIXED
        assert normalization_method(IndicatorCode.DEMAND_STABILITY_SCORE) == NormalizationMethod.MINMAX_FIXED

    def test_reference_bounds_indicators(self) -> None:
        assert normalization_method(IndicatorCode.GDP_PER_CAPITA_SCORE) == NormalizationMethod.MINMAX_REFERENCE
        assert normalization_method(IndicatorCode.NET_MIGRATION_RATE_SCORE) == NormalizationMethod.MINMAX_REFERENCE


class TestNormalize:
    def test_passthrough_returns_raw_value_unchanged(self) -> None:
        score, note = normalize(
            indicator_code=IndicatorCode.TREND_SCORE, raw_value=60.11, direction=IndicatorDirection.DIRECT,
            reference=None,
        )  # fmt: skip
        assert score == 60.11
        assert note is None

    def test_passthrough_clamps_out_of_range(self) -> None:
        score, _ = normalize(
            indicator_code=IndicatorCode.URBANIZATION_SCORE, raw_value=105, direction=IndicatorDirection.DIRECT,
            reference=None,
        )  # fmt: skip
        assert score == 100.0

    def test_minmax_direct_direction(self) -> None:
        reference = _reference(0, 100)
        score, note = normalize(
            indicator_code=IndicatorCode.GDP_PER_CAPITA_SCORE, raw_value=25, direction=IndicatorDirection.DIRECT,
            reference=reference,
        )  # fmt: skip
        assert score == 25.0
        assert note is None

    def test_minmax_inverse_direction(self) -> None:
        reference = _reference(0, 10)
        score, _ = normalize(
            indicator_code=IndicatorCode.WORLDBANK_INFLATION_SCORE, raw_value=2, direction=IndicatorDirection.INVERSE,
            reference=reference,
        )  # fmt: skip
        assert score == 80.0  # ((10 - 2) / 10) * 100

    def test_missing_reference_returns_none_with_note(self) -> None:
        score, note = normalize(
            indicator_code=IndicatorCode.GDP_GROWTH_SCORE, raw_value=3.2, direction=IndicatorDirection.DIRECT,
            reference=None,
        )  # fmt: skip
        assert score is None
        assert note is not None and "no normalization_reference row" in note

    def test_degenerate_reference_returns_fifty(self) -> None:
        reference = _reference(5, 5)
        score, note = normalize(
            indicator_code=IndicatorCode.GDP_PER_CAPITA_SCORE, raw_value=5, direction=IndicatorDirection.DIRECT,
            reference=reference,
        )  # fmt: skip
        assert score == 50.0
        assert note is not None and "max(X)=min(X)" in note

    def test_minmax_clamps_values_outside_percentile_bounds(self) -> None:
        reference = _reference(10, 20)
        score, _ = normalize(
            indicator_code=IndicatorCode.GDP_PER_CAPITA_SCORE, raw_value=1000, direction=IndicatorDirection.DIRECT,
            reference=reference,
        )  # fmt: skip
        assert score == 100.0

        score, _ = normalize(
            indicator_code=IndicatorCode.GDP_PER_CAPITA_SCORE, raw_value=-5, direction=IndicatorDirection.DIRECT,
            reference=reference,
        )  # fmt: skip
        assert score == 0.0

    def test_fixed_bounds_growth_score(self) -> None:
        reference = _reference(-50, 150)  # matches migration 005 seed
        score, _ = normalize(
            indicator_code=IndicatorCode.GROWTH_SCORE, raw_value=50, direction=IndicatorDirection.DIRECT,
            reference=reference,
        )  # fmt: skip
        assert score == 50.0  # (50 - (-50)) / (150 - (-50)) * 100 = 100/200*100 = 50
