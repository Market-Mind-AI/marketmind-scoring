"""Rank Order Centroid (ROC) — converts an explicit priority ranking (Barron
& Barrett, 1996, "Decision Quality Using Ranked Attribute Weights",
Management Science) into dimension weights, replacing Entropy Weight as the
production weighting method (decision of 2026-07-03, see memory
marketmind_mos_engine). Live testing showed Entropy Weight repeatedly
undervaluing Demand (2.6%-5% of the total weight, even against a real
12-country sample) purely because search interest for the tested product
happened to vary little across the reference countries — "doesn't
discriminate between markets in this sample" is not the same as "doesn't
matter", especially for a tool whose whole purpose is judging a specific
product's market potential. ROC is the standard technique for exactly this
situation: the decision-maker (the user, grounded in the product's actual
purpose) knows the priority ORDER with confidence but not exact
proportions — it is not an arbitrary hand-picked number, it is a
mathematically derived consequence of that order.

w_j = (1/n) * sum(1/k for k in j..n), for rank j = 1..n (1 = most important)
"""

from app.domain.enums import IndicatorDimension

# Explicit priority order, most to least important for judging a specific
# product's market potential in a country (user decision, 2026-07-03):
# 1. Demand — the most direct, product-specific signal (people searching
#    for this exact product); everything else is a proxy.
# 2. Economic Readiness — purchasing power is the strongest real
#    differentiator once a market clears a basic digital-access threshold
#    (most of the reference population already has decent internet).
# 3. Market Size — scale of the addressable audience.
# 4. Digital Readiness — largely saturated across the reference population
#    already (e.g. Spain 95.8%, USA 94.7% internet penetration), and
#    partially redundant with Demand itself (PyTrends search volume already
#    depends on internet access, so weighting this heavily double-counts
#    that effect).
# 5. Demographic Sustainability — long-term population trend, the least
#    tied to "will this sell now", which is what this app answers.
PRIORITY_ORDER: list[IndicatorDimension] = [
    IndicatorDimension.DEMAND,
    IndicatorDimension.ECONOMIC_READINESS,
    IndicatorDimension.MARKET_SIZE,
    IndicatorDimension.DIGITAL_READINESS,
    IndicatorDimension.DEMOGRAPHIC_SUSTAINABILITY,
]


def roc_weights(order: list[IndicatorDimension]) -> dict[IndicatorDimension, float]:
    """order[0] is the most important dimension, order[-1] the least.
    Returns weights summing to 1."""
    n = len(order)
    return {dimension: sum(1 / k for k in range(rank, n + 1)) / n for rank, dimension in enumerate(order, start=1)}
