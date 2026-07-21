"""Cap. 5 §5.5 correction — 2-stage aggregation: the ~16 atomic indicators
(Cap. 4) are grouped into 5 dimensions before Entropy Weight runs, so Demand
(5 indicators) isn't diluted against the macro block (11 indicators) just
for having fewer atomic signals. See memory marketmind_mos_engine."""

from app.db.models.indicator_value import IndicatorValue
from app.domain.enums import AvailabilityStatus, IndicatorCode, IndicatorDimension

DIMENSION_BY_INDICATOR: dict[IndicatorCode, IndicatorDimension] = {
    # DEMAND split into VOLUME (absolute market size / sales potential) and
    # QUALITY (shape signals) — 2026-07-16. trend_score is the absolute
    # search-volume LEVEL (linear-normalized), the sole VOLUME signal; the
    # other three are size-independent quality ratios. Averaging them together
    # let a tiny high-quality market tie a huge one; separating makes volume a
    # dimension of its own so it isn't diluted. See marketmind_mos_engine.
    IndicatorCode.TREND_SCORE: IndicatorDimension.DEMAND_VOLUME,
    IndicatorCode.GROWTH_SCORE: IndicatorDimension.DEMAND_QUALITY,
    IndicatorCode.DEMAND_STABILITY_SCORE: IndicatorDimension.DEMAND_QUALITY,
    IndicatorCode.SEARCH_INTENT_SCORE: IndicatorDimension.DEMAND_QUALITY,
    # REGIONAL_DEMAND_SCORE is deliberately NOT scored into Demand (2026-07-14):
    # it's the per-country `interest_by_region` mean, self-scaled to each
    # country's OWN top region, so it is NOT cross-country comparable (a
    # geographically-spread market reads high, a concentrated one low, on
    # different rulers) — mixing it into a cross-country MOS distorted the
    # ranking (e.g. it inflated Mexico over the USA for air fryer). The other
    # four Demand signals ARE comparable: trend_score/search_intent are
    # worldwide/composition, growth/stability are scale-invariant ratios.
    # regional_demand is still computed and stored as narrative CONTEXT for the
    # LLM (like government_expenditure), just never aggregated. See
    # marketmind_mos_engine.
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
# Only two indicators are non-mandatory, and both for structural reasons (not
# collection fragility):
#   - Median Age Fit: only defined when the Product Profile has a target age
#     range (Cap. 4 §4.5.3) — conditional by design.
#   - Real Estate Growth: OECD-only coverage (Cap. 4 §4.4.3) — non-OECD
#     countries structurally cannot have it.
# Everything else — including ALL five Demand signals (trend_score plus the
# four per-country PyTrends signals) — is mandatory (user principle 2026-07-11:
# the MOS must never be computed on partial data; every country carries the
# SAME indicator set on the same 0-100 ruler, so the scores stay comparable).
# Collection must therefore complete for every country before any score/MOS is
# computed (see analysis_service completeness gate); PyTrends rate limits are
# handled by reliable retry + the DataForSEO live fallback, never by skipping.
MANDATORY_INDICATORS: frozenset[IndicatorCode] = frozenset(DIMENSION_BY_INDICATOR) - {
    IndicatorCode.MEDIAN_AGE_FIT_SCORE,
    IndicatorCode.REAL_ESTATE_GROWTH_SCORE,
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
    available indicator in that dimension, all on the same 0-100 ruler
    (Median Age Fit only contributes when present — it's conditional, not
    mandatory). Demand averages its FOUR scored signals like any other
    dimension: every one is mandatory and collected for every country before
    this runs (completeness gate), so the set is uniform and the plain mean
    stays comparable across countries. All four are cross-country comparable —
    trend_score comes from the WORLDWIDE breakdown, search_intent is a
    composition, and growth/stability are scale-invariant ratios.
    regional_demand is intentionally excluded (self-scaled per country, not
    comparable — see DIMENSION_BY_INDICATOR); it stays as narrative context."""
    by_dimension: dict[IndicatorDimension, list[float]] = {}
    for code, dimension in DIMENSION_BY_INDICATOR.items():
        iv = indicator_values.get(code.value)
        if iv is None or iv.normalized_score is None:
            continue
        by_dimension.setdefault(dimension, []).append(float(iv.normalized_score))
    return {dimension: sum(scores) / len(scores) for dimension, scores in by_dimension.items() if scores}
