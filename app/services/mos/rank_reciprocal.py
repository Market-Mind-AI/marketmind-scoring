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
# 2026-07-08, and 2026-07-10):
# 1. Demand — the most direct, product-specific signal (how much this exact
#    product is actually wanted here); everything else is a proxy. Now
#    cross-country comparable via the worldwide Google Trends signal (see
#    pytrends.py compute_trend_score / interest_by_country).
# 2. Macro Demand Structure — the country's aggregate demand composition
#    (GDP = C+I+G+NX); ranked right after Demand because Consumer Spending
#    is the closest macro proxy to "people actually spending money here".
# 3. Digital Readiness — can the audience discover/buy the product online;
#    product-relevant and moved up over the generic "big rich country"
#    signals below.
# 4. Economic Readiness — purchasing power + macro stability. Moved DOWN
#    (2026-07-10, user decision): it's dominated by GDP per capita, which
#    systematically rewarded large wealthy countries regardless of product
#    fit (e.g. it was a main reason the USA outranked Spain for jamón).
#    Reducing its rank cuts that generic-wealth bias without hand-picking
#    weights (the RR formula does it from the order alone).
# 5. Demographic Sustainability — long-term population trend, loosely tied
#    to "will this sell now".
# 6. Market Size — raw population/urbanization scale, the most "generic big
#    country" signal (a large population isn't product fit). Moved to LAST
#    (2026-07-10, user decision) for the same generic-size-bias reason as
#    Economic Readiness above.
PRIORITY_ORDER: list[IndicatorDimension] = [
    IndicatorDimension.DEMAND,
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
