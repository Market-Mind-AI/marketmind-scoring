"""Rank Reciprocal (RR) — converts an explicit priority ranking into
dimension weights, replacing Rank Order Centroid (ROC) as the production
weighting method (decision of 2026-07-08, see memory marketmind_mos_engine
— supersedes app/services/mos/roc_weights.py, used from 2026-07-03 to
2026-07-08). Same family of "surrogate weighting" techniques as ROC (Barron
& Barrett, 1996; both compared in Roberts & Goodwin, 2002,
"Weight approximations in multi-attribute decision models", Journal of
Multi-Criteria Decision Analysis) — the order itself is still a non-
statistical, explicit priority decision, never recalculated from data.

Why the switch: adding the 6th dimension (Macro Demand Structure) forced a
decision on where Market Size and Economic Readiness rank relative to each
other. The user wanted their weights closer together — but manually
nudging the ROC numbers would have reintroduced exactly the "arbitrary
weight" problem that motivated abandoning Entropy Weight in the first
place (Cap. 4: "sin pesos arbitrarios"). Rank Reciprocal is a different,
equally citable formula for the same ordered-priority input: it happens to
keep Demand's weight almost unchanged (40.82% vs. ROC's 40.83%) while
narrowing the Market Size / Economic Readiness gap (5.55pp under ROC ->
3.41pp under RR), which is what was actually being asked for — a category
change (different formula), not a parameter tweak (hand-picked numbers).

w_j = (1/j) / sum(1/k for k in 1..n), for rank j = 1..n (1 = most important)
"""

from app.domain.enums import IndicatorDimension

# Explicit priority order, most to least important for judging a specific
# product's market potential in a country (user decisions 2026-07-03,
# 2026-07-08, 2026-07-10, and 2026-07-16):
# 1. Demand VOLUME — the absolute size of real, product-specific demand (how
#    many people actually search to buy this product here). This is the most
#    direct sales-potential signal and, per the 2026-07-16 redesign, the top
#    priority: a market with far more interested buyers must outrank one with
#    better macro conditions but little demand. Linear-normalized (proportional
#    to volume), so the ranking tracks real sales headroom (see pytrends.py
#    compute_trend_score).
# 2. Demand QUALITY — is that demand growing, stable, and purchase-oriented
#    (vs merely curious)? Size-independent shape signals; ranked right after
#    volume so a small-but-excellent market is still strongly rewarded here
#    without letting its quality erase a large market's volume advantage.
# 3. Macro Demand Structure — the country's aggregate demand composition
#    (GDP = C+I+G+NX); Consumer Spending is the closest macro proxy to
#    "people actually spending money here".
# 4. Digital Readiness — can the audience discover/buy the product online.
# 5. Economic Readiness — purchasing power + macro stability. Kept below the
#    demand/structure signals (2026-07-10): it's dominated by GDP per capita,
#    which systematically rewarded large wealthy countries regardless of
#    product fit (a main reason the USA outranked Spain for jamón).
# 6. Demographic Sustainability — long-term population trend, loosely tied
#    to "will this sell now".
# 7. Market Size — raw population/urbanization scale, the most "generic big
#    country" signal. LAST because a large population isn't product fit, and
#    product-specific size is already captured, far better, by Demand Volume
#    above (raw population would double-count size with non-interested people).
PRIORITY_ORDER: list[IndicatorDimension] = [
    IndicatorDimension.DEMAND_VOLUME,
    IndicatorDimension.DEMAND_QUALITY,
    IndicatorDimension.MACRO_DEMAND_STRUCTURE,
    IndicatorDimension.DIGITAL_READINESS,
    IndicatorDimension.ECONOMIC_READINESS,
    IndicatorDimension.DEMOGRAPHIC_SUSTAINABILITY,
    IndicatorDimension.MARKET_SIZE,
]


def rank_reciprocal_weights(order: list[IndicatorDimension]) -> dict[IndicatorDimension, float]:
    """order[0] is the most important dimension, order[-1] the least.
    Returns weights summing to 1."""
    n = len(order)
    denominator = sum(1 / k for k in range(1, n + 1))
    return {dimension: (1 / rank) / denominator for rank, dimension in enumerate(order, start=1)}
