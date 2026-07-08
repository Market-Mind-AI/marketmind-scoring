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
# product's market potential in a country (user decisions, 2026-07-03 and
# 2026-07-08):
# 1. Demand — the most direct, product-specific signal (people searching
#    for this exact product); everything else is a proxy.
# 2. Macro Demand Structure — the country's aggregate demand composition
#    (GDP = C+I+G+NX); ranked right after Demand because Consumer Spending
#    is the closest macro proxy to "people actually spending money here".
# 3. Market Size — scale of the addressable audience. Ranked above Economic
#    Readiness (swapped 2026-07-08, user decision): with Consumer Spending
#    now a dedicated signal one rank above, part of what used to justify
#    "purchasing power over audience size" is already covered there, so
#    audience size gets relatively more weight.
# 4. Economic Readiness — purchasing power/momentum, still a real
#    differentiator, just no longer ranked above Market Size.
# 5. Digital Readiness — largely saturated across the reference population
#    already (e.g. Spain 95.8%, USA 94.7% internet penetration), and
#    partially redundant with Demand itself (PyTrends search volume already
#    depends on internet access, so weighting this heavily double-counts
#    that effect).
# 6. Demographic Sustainability — long-term population trend, the least
#    tied to "will this sell now", which is what this app answers.
PRIORITY_ORDER: list[IndicatorDimension] = [
    IndicatorDimension.DEMAND,
    IndicatorDimension.MACRO_DEMAND_STRUCTURE,
    IndicatorDimension.MARKET_SIZE,
    IndicatorDimension.ECONOMIC_READINESS,
    IndicatorDimension.DIGITAL_READINESS,
    IndicatorDimension.DEMOGRAPHIC_SUSTAINABILITY,
]


def rank_reciprocal_weights(order: list[IndicatorDimension]) -> dict[IndicatorDimension, float]:
    """order[0] is the most important dimension, order[-1] the least.
    Returns weights summing to 1."""
    n = len(order)
    denominator = sum(1 / k for k in range(1, n + 1))
    return {dimension: (1 / rank) / denominator for rank, dimension in enumerate(order, start=1)}
