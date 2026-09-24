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
        IndicatorCode.BROADBAND_ADOPTION_SCORE,
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
# Category Volume é o volume absoluto de busca da CATEGORIA do produto, medido
# ao lado da marca para dar escala à narrativa. Não é normalizado porque não é
# pontuado: a régua de demanda (o teto do mercado de referência) é da MARCA, e
# aplicá-la aqui produziria um número que parece comparável ao Demand Volume
# sem ser — os dois medem assuntos diferentes. Fica como raw_value + nota.
_CONTEXT_ONLY_INDICATORS = frozenset(
    {
        IndicatorCode.GOVERNMENT_EXPENDITURE_SCORE,
        IndicatorCode.CATEGORY_VOLUME_SCORE,
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
#
# The macro indicators below are also fixed (never self-widen) because they have
# universal, economically-anchored ranges and are exposed to real extreme
# outliers: a single hyperinflation country (Argentina ~220%) self-widened the
# inflation ceiling to 220% and crushed every normal economy (2–5%) into ~96,
# indistinguishable (2026-07-24). With a fixed, economically-sane range the
# extreme SATURATES at the worst score (0) instead of stretching the ruler for
# everyone; the yardstick stays stable and comparable across analyses. Their
# ranges are set by migration 020.
#
# The remaining macro indicators (2026-07-24) are blinded the same way: their
# fixed ranges are already economically sane (round seeds from migrations
# 005/010/012, not corrupted), so no reseed is needed — but without this a
# future extreme (e.g. Singapore's ~350% trade openness, an oil exporter's net
# exports, a post-crisis GDP swing) would widen the ceiling and retroactively
# crush every normal country. Now such extremes SATURATE at 0/100 instead.
_NO_SELF_WIDEN: frozenset[IndicatorCode] = frozenset(
    {
        IndicatorCode.TREND_SCORE,
        IndicatorCode.WORLDBANK_INFLATION_SCORE,
        IndicatorCode.REAL_ESTATE_GROWTH_SCORE,
        IndicatorCode.DEMAND_STABILITY_SCORE,
        IndicatorCode.GDP_GROWTH_SCORE,
        IndicatorCode.NET_EXPORTS_SCORE,
        IndicatorCode.INVESTMENT_SCORE,
        IndicatorCode.CONSUMER_SPENDING_SCORE,
        IndicatorCode.TRADE_OPENNESS_SCORE,
        IndicatorCode.GDP_PER_CAPITA_SCORE,
        # Final batch (2026-07-27): every remaining fixed-bound indicator is
        # frozen too, so NO ruler self-widens anymore. Self-widening at different
        # times had left scores on different rulers (mobile_connectivity came out
        # non-monotonic across countries — a stale-ruler discrepancy). With all
        # rulers fixed + a one-off recompute, every country sits on one yardstick.
        IndicatorCode.GROWTH_SCORE,
        IndicatorCode.MOBILE_CONNECTIVITY_SCORE,
        # Broadband Adoption nasce congelado (2026-09-24) pelo mesmo motivo dos
        # demais: se a régua se alargasse sozinha, o primeiro país fora da faixa
        # esmagaria retroativamente todo mundo que já foi pontuado. E aqui isso
        # importaria mais que o normal, porque o indicador entra com cobertura
        # ainda em backfill -- a ordem em que os países chegam não pode mudar o
        # resultado de quem já chegou.
        IndicatorCode.BROADBAND_ADOPTION_SCORE,
        IndicatorCode.NET_MIGRATION_RATE_SCORE,
        IndicatorCode.POPULATION_GROWTH_SCORE,
        IndicatorCode.POPULATION_SCALE_SCORE,
    }
)


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


def normalize_demand_volume(raw_value: Decimal | float, ceiling: float) -> tuple[float | None, str | None]:
    """Demand Volume (trend_score) normalized against the PRODUCT'S REFERENCE
    MARKET volume as the 100-point ceiling (Cap. 4 §4.2.1, 2026-07-22): score =
    value / ceiling x 100, saturating at 100. Linear (proportional to volume),
    per-product ruler — replaces the fixed 2M ceiling so a niche product isn't
    floored. The caller supplies the reference-market volume (or a fallback
    ceiling) and only uses this when ceiling > 0."""
    value = float(raw_value)
    if ceiling <= 0:
        return None, "Demand Volume reference-market ceiling <= 0 (cannot normalize)"
    return max(0.0, min(100.0, value / ceiling * 100)), None


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
