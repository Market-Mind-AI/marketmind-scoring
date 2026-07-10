"""Cap. 5 §5.5 correction — 2-stage aggregation: the ~16 atomic indicators
(Cap. 4) are grouped into 5 dimensions before Entropy Weight runs, so Demand
(5 indicators) isn't diluted against the macro block (11 indicators) just
for having fewer atomic signals. See memory marketmind_mos_engine."""

from app.db.models.indicator_value import IndicatorValue
from app.domain.enums import AvailabilityStatus, IndicatorCode, IndicatorDimension

DIMENSION_BY_INDICATOR: dict[IndicatorCode, IndicatorDimension] = {
    IndicatorCode.TREND_SCORE: IndicatorDimension.DEMAND,
    IndicatorCode.GROWTH_SCORE: IndicatorDimension.DEMAND,
    IndicatorCode.DEMAND_STABILITY_SCORE: IndicatorDimension.DEMAND,
    IndicatorCode.REGIONAL_DEMAND_SCORE: IndicatorDimension.DEMAND,
    IndicatorCode.SEARCH_INTENT_SCORE: IndicatorDimension.DEMAND,
    IndicatorCode.INTERNET_PENETRATION_SCORE: IndicatorDimension.DIGITAL_READINESS,
    IndicatorCode.MOBILE_CONNECTIVITY_SCORE: IndicatorDimension.DIGITAL_READINESS,
    IndicatorCode.GDP_PER_CAPITA_SCORE: IndicatorDimension.ECONOMIC_READINESS,
    IndicatorCode.GDP_GROWTH_SCORE: IndicatorDimension.ECONOMIC_READINESS,
    IndicatorCode.WORLDBANK_INFLATION_SCORE: IndicatorDimension.ECONOMIC_READINESS,
    IndicatorCode.REAL_ESTATE_GROWTH_SCORE: IndicatorDimension.ECONOMIC_READINESS,
    IndicatorCode.POPULATION_SCALE_SCORE: IndicatorDimension.MARKET_SIZE,
    IndicatorCode.URBANIZATION_SCORE: IndicatorDimension.MARKET_SIZE,
    IndicatorCode.POPULATION_GROWTH_SCORE: IndicatorDimension.DEMOGRAPHIC_SUSTAINABILITY,
    IndicatorCode.NET_MIGRATION_RATE_SCORE: IndicatorDimension.DEMOGRAPHIC_SUSTAINABILITY,
    IndicatorCode.MEDIAN_AGE_FIT_SCORE: IndicatorDimension.DEMOGRAPHIC_SUSTAINABILITY,
    # Macro Demand Structure (2026-07-08, GDP = C+I+G+NX) — Government
    # Expenditure (G) is deliberately NOT here: it's context-only (no
    # defensible mayor/menor direction), never enters a dimension. See
    # marketmind_mos_engine.
    IndicatorCode.CONSUMER_SPENDING_SCORE: IndicatorDimension.MACRO_DEMAND_STRUCTURE,
    IndicatorCode.INVESTMENT_SCORE: IndicatorDimension.MACRO_DEMAND_STRUCTURE,
    IndicatorCode.NET_EXPORTS_SCORE: IndicatorDimension.MACRO_DEMAND_STRUCTURE,
    IndicatorCode.TRADE_OPENNESS_SCORE: IndicatorDimension.MACRO_DEMAND_STRUCTURE,
}

# Mandatory = present or the country is excluded from the MOS (Cap. 5 §5.3).
# Non-mandatory exclusions:
#   - Median Age Fit: only required when the Product Profile has a target age
#     range (Cap. 4 §4.5.3).
#   - Real Estate Growth: OECD-only coverage (Cap. 4 §4.4.3).
#   - Growth / Demand Stability / Regional Demand / Search Intent: the four
#     per-country PyTrends *enrichment* signals (2026-07-10). They're
#     collection-fragile — Google Trends rate-limits per-country queries and
#     the DataForSEO sandbox fallback returns no region/query data, so any
#     one of them can transiently come back empty and would otherwise
#     exclude an entire country. trend_score (the WORLDWIDE interest-by-
#     country anchor, one reliable call per keyword) stays mandatory as the
#     Demand dimension's floor; the Demand sub-score averages whichever of
#     these enrichment signals are present. Inflation
#     (WORLDBANK_INFLATION_SCORE) is mandatory like any other WB indicator.
MANDATORY_INDICATORS: frozenset[IndicatorCode] = frozenset(DIMENSION_BY_INDICATOR) - {
    IndicatorCode.MEDIAN_AGE_FIT_SCORE,
    IndicatorCode.REAL_ESTATE_GROWTH_SCORE,
    IndicatorCode.GROWTH_SCORE,
    IndicatorCode.DEMAND_STABILITY_SCORE,
    IndicatorCode.REGIONAL_DEMAND_SCORE,
    IndicatorCode.SEARCH_INTENT_SCORE,
}


def _is_present(indicator_values: dict[str, IndicatorValue], code: IndicatorCode) -> bool:
    iv = indicator_values.get(code.value)
    if iv is None or iv.normalized_score is None:
        return False
    return iv.availability_status != AvailabilityStatus.UNAVAILABLE


def check_availability(indicator_values: dict[str, IndicatorValue]) -> str | None:
    """Cap. 5 §5.3: every mandatory indicator must be present. Returns None
    if the country is eligible for MOS, or a human-readable exclusion
    reason otherwise (never silently drops the country)."""
    missing = [code.value for code in MANDATORY_INDICATORS if not _is_present(indicator_values, code)]
    if missing:
        return "Missing mandatory indicator(s): " + ", ".join(missing)
    return None


def compute_dimension_subscores(indicator_values: dict[str, IndicatorValue]) -> dict[IndicatorDimension, float]:
    """Sub-score per dimension = mean of the normalized scores of every
    available indicator in that dimension (Median Age Fit only contributes
    when present — it's conditional, not mandatory)."""
    by_dimension: dict[IndicatorDimension, list[float]] = {}
    for code, dimension in DIMENSION_BY_INDICATOR.items():
        iv = indicator_values.get(code.value)
        if iv is None or iv.normalized_score is None:
            continue
        by_dimension.setdefault(dimension, []).append(float(iv.normalized_score))
    return {dimension: sum(scores) / len(scores) for dimension, scores in by_dimension.items() if scores}
