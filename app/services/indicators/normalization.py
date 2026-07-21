"""Cap. 4 §4.1 normalization step — runs after a raw-value function (see
pytrends.py/worldbank.py/oecd.py/undata.py).

Every indicator normalizes against a fixed, absolute bound
(normalization_reference, period_key="FIXED") — never against the other
countries in the current query (decision 2026-07-08: this product only ever
collects data for the countries a user actually requests, so a percentile-
based reference population never populates organically without forcing
every analysis to cover all reference countries, which defeats the point of
per-country selection). A country's score for a given indicator is the same
whether it's analyzed alone or alongside 20 other countries in the same
/analyze call. Fixed bounds self-widen (never narrow) the first time a real
observed value falls outside today's range — see
indicator_engine.py::_normalize_and_save and
indicator_repository.py::widen_reference_bounds — so the initial bounds
below only need to be plausible starting points, not perfectly researched
limits.
"""

from decimal import Decimal

from app.db.models.normalization_reference import NormalizationReference
from app.domain.enums import IndicatorCode, IndicatorDirection, NormalizationMethod

FIXED_BOUNDS_PERIOD_KEY = "FIXED"

# TREND_SCORE (the Demand VOLUME level) is NOT passthrough: it is the absolute
# Google Ads monthly search volume, normalized LINEARLY against a fixed ceiling
# (2026-07-16 redesign) so the score is proportional to volume. See
# _NO_SELF_WIDEN below — its ceiling is deliberately fixed (never self-widens).
_PASSTHROUGH_INDICATORS = frozenset(
    {
        IndicatorCode.SEARCH_INTENT_SCORE,
        IndicatorCode.INTERNET_PENETRATION_SCORE,
        IndicatorCode.URBANIZATION_SCORE,
        IndicatorCode.MEDIAN_AGE_FIT_SCORE,
    }
)

# Every indicator not in _PASSTHROUGH_INDICATORS or _CONTEXT_ONLY_INDICATORS
# lands here — see normalization_method()'s fallback. Initial bounds seeded
# in alembic/versions/010_fixed_normalization_bounds_v2.py (and 005 for
# GROWTH_SCORE/DEMAND_STABILITY_SCORE, seeded before this became the only
# normalization method for non-passthrough indicators).
_FIXED_BOUNDS_INDICATORS = frozenset(
    {
        IndicatorCode.TREND_SCORE,
        IndicatorCode.GROWTH_SCORE,
        IndicatorCode.DEMAND_STABILITY_SCORE,
        IndicatorCode.GDP_PER_CAPITA_SCORE,
        IndicatorCode.GDP_GROWTH_SCORE,
        IndicatorCode.POPULATION_SCALE_SCORE,
        IndicatorCode.MOBILE_CONNECTIVITY_SCORE,
        IndicatorCode.WORLDBANK_INFLATION_SCORE,
        IndicatorCode.POPULATION_GROWTH_SCORE,
        IndicatorCode.NET_MIGRATION_RATE_SCORE,
        IndicatorCode.CONSUMER_SPENDING_SCORE,
        IndicatorCode.INVESTMENT_SCORE,
        IndicatorCode.NET_EXPORTS_SCORE,
        IndicatorCode.TRADE_OPENNESS_SCORE,
        IndicatorCode.REAL_ESTATE_GROWTH_SCORE,
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


# Indicators whose fixed bound must NOT self-widen. TREND_SCORE (Demand Volume)
# uses a fixed ceiling representing "maximal addressable national demand": a
# market at/above it saturates at 100, everything else scales proportionally
# below. Self-widening would break this — a single mega-volume product would
# push the global ceiling up and RETROACTIVELY shrink every other product's
# volume score, making the demand dimension unstable across analyses. So the
# ceiling stays fixed and values above it saturate (2026-07-16). Linearity is
# the founded property; the ceiling is a documented scale anchor (migration 014).
_NO_SELF_WIDEN: frozenset[IndicatorCode] = frozenset({IndicatorCode.TREND_SCORE})


def self_widens(indicator_code: IndicatorCode) -> bool:
    """Whether a MINMAX_FIXED indicator's bound self-widens on out-of-range
    observations. False for fixed-ceiling indicators (see _NO_SELF_WIDEN)."""
    return normalization_method(indicator_code) == NormalizationMethod.MINMAX_FIXED and (
        indicator_code not in _NO_SELF_WIDEN
    )


def normalization_method(indicator_code: IndicatorCode) -> NormalizationMethod:
    if indicator_code in _PASSTHROUGH_INDICATORS:
        return NormalizationMethod.PASSTHROUGH
    if indicator_code in _CONTEXT_ONLY_INDICATORS:
        return NormalizationMethod.CONTEXT_ONLY
    return NormalizationMethod.MINMAX_FIXED


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
