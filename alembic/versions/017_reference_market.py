"""product_catalog.reference_market + refresh Demand Volume meaning

2026-07-22 (user request). Demand Volume (trend_score) is no longer normalized
against a fixed global 2M-search ceiling — that floored every niche/premium
product. Instead each product carries a REFERENCE MARKET: the single supported
country with the highest online search demand for it (chosen by the enrichment
LLM, with its own localized terms). Its absolute search volume is the ceiling of
the Demand Volume ruler for that product:

    trend_score(country) = country_volume / reference_market_volume x 100  (<=100)

so the ruler fits the product and Demand Volume regains its differentiating
power. The reference market is never scored/ranked itself — it is the ruler
anchor and is explained by the final LLM. The old fixed bound (migration 014)
stays as a last-resort fallback when no reference market can be determined or
collected. This migration adds the JSONB column and refreshes the trend_score
meaning row in metric_explanations (migration 016) to describe the new ruler.

Revision ID: 017
Revises: 016
Create Date: 2026-07-22

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "017"
down_revision: str | None = "016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TREND_MEANING_NEW = (
    "DEMAND VOLUME — the total absolute monthly online searches to buy this product in this country "
    "(the raw_value is that real count). This is market POTENTIAL / sales headroom, normalized against "
    "the PRODUCT'S REFERENCE MARKET (its highest-demand country) as the 100-point ceiling: the score is "
    "this country's demand as a percentage of the product's biggest national market, so a niche product "
    "is measured on its own scale instead of a fixed global one, and 2x the searches ~ 2x the score. "
    "IMPORTANT for the narrative: a large volume can come from (a) sheer population or (b) genuine "
    "appetite (a high share of the population wanting it) — read this together with the 'penetration' "
    "figure in notes (searches per 100k inhabitants) to tell them apart. High volume AND high penetration "
    "= big AND enthusiastic market; high volume but low penetration = big only because populous; low "
    "volume but high penetration = small but strong appetite (real potential per person)."
)

_TREND_MEANING_OLD = (
    "DEMAND VOLUME — the total absolute monthly online searches to buy this product in this country "
    "(the raw_value is that real count). This is market POTENTIAL / sales headroom and is normalized "
    "proportionally: 2x the searches ~ 2x the score. A high score means many people actually want this "
    "product here. IMPORTANT for the narrative: a large volume can come from (a) sheer population or "
    "(b) genuine appetite (a high share of the population wanting it) — read this together with the "
    "'penetration' figure in notes (searches per 100k inhabitants) to tell them apart. High volume AND "
    "high penetration = big AND enthusiastic market; high volume but low penetration = big only because "
    "populous; low volume but high penetration = small but strong appetite (real potential per person)."
)


def _set_trend_meaning(text: str) -> None:
    op.execute(
        sa.text("UPDATE metric_explanations SET meaning = :m WHERE metric_code = 'trend_score'").bindparams(m=text)
    )


def upgrade() -> None:
    op.add_column("product_catalog", sa.Column("reference_market", postgresql.JSONB(), nullable=True))
    _set_trend_meaning(_TREND_MEANING_NEW)


def downgrade() -> None:
    _set_trend_meaning(_TREND_MEANING_OLD)
    op.drop_column("product_catalog", "reference_market")
