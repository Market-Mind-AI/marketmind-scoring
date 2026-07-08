"""Cap. 4 §4.1 normalization step — runs after a raw-value function (see
pytrends.py/worldbank.py/oecd.py/undata.py). Correction adopted over the
literal Cap. 4 text (docs/marketmind-implementation-plan.md section 7,
memory marketmind_mos_engine): bounds come from a fixed reference
population (normalization_reference), never from the countries in the
current query — that's what makes MOS absolute and comparable across runs.
"""

from decimal import Decimal

from app.db.models.normalization_reference import NormalizationReference
from app.domain.enums import IndicatorCode, IndicatorDirection, NormalizationMethod

FIXED_BOUNDS_PERIOD_KEY = "FIXED"

_PASSTHROUGH_INDICATORS = frozenset(
    {
        IndicatorCode.TREND_SCORE,
        IndicatorCode.REGIONAL_DEMAND_SCORE,
        IndicatorCode.SEARCH_INTENT_SCORE,
        IndicatorCode.INTERNET_PENETRATION_SCORE,
        IndicatorCode.URBANIZATION_SCORE,
        IndicatorCode.MEDIAN_AGE_FIT_SCORE,
    }
)

_FIXED_BOUNDS_INDICATORS = frozenset(
    {
        IndicatorCode.GROWTH_SCORE,
        IndicatorCode.DEMAND_STABILITY_SCORE,
    }
)

# Government Expenditure has no defensible mayor/menor direction (confirmed
# against live World Bank data — France/Denmark ~23-24% of GDP, among the
# highest in the world, vs. India ~11% — reflects economic model, not
# consumer-market quality). Never normalized, never enters a dimension;
# raw_value is kept purely as LLM context. See marketmind_mos_engine.
_CONTEXT_ONLY_INDICATORS = frozenset(
    {
        IndicatorCode.GOVERNMENT_EXPENDITURE_SCORE,
    }
)


def normalization_method(indicator_code: IndicatorCode) -> NormalizationMethod:
    if indicator_code in _PASSTHROUGH_INDICATORS:
        return NormalizationMethod.PASSTHROUGH
    if indicator_code in _FIXED_BOUNDS_INDICATORS:
        return NormalizationMethod.MINMAX_FIXED
    if indicator_code in _CONTEXT_ONLY_INDICATORS:
        return NormalizationMethod.CONTEXT_ONLY
    return NormalizationMethod.MINMAX_REFERENCE


def normalize(
    *,
    indicator_code: IndicatorCode,
    raw_value: Decimal | float,
    direction: IndicatorDirection,
    reference: NormalizationReference | None,
) -> tuple[float | None, str | None]:
    """Returns (normalized_score, note) — note is set whenever the score is
    None or clamped/defaulted, so a caller never silently loses the reason."""
    method = normalization_method(indicator_code)
    value = float(raw_value)

    if method == NormalizationMethod.CONTEXT_ONLY:
        return None, "context-only indicator — no defensible mayor/menor direction, not scored (Cap. 4 §4.3.x)"

    if method == NormalizationMethod.PASSTHROUGH:
        return max(0.0, min(100.0, value)), None

    if reference is None:
        return None, f"no normalization_reference row for {indicator_code.value} (period missing or not computed yet)"

    min_x, max_x = float(reference.min_x), float(reference.max_x)
    if max_x == min_x:
        return 50.0, "max(X)=min(X) in reference bounds — indicator doesn't differentiate markets (Cap. 4 §4.1)"

    if direction == IndicatorDirection.DIRECT:
        score = ((value - min_x) / (max_x - min_x)) * 100
    else:
        score = ((max_x - value) / (max_x - min_x)) * 100
    return max(0.0, min(100.0, score)), None
