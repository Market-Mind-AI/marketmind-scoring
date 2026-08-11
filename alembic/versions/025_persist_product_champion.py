"""Persist the reference-market champion on the product.

The champion is the best-MOS market among a product's reference candidates. It is
a property of the PRODUCT, not of a request: MOS uses fixed 100/0 TOPSIS anchors,
so scoring a candidate gives the same answer whoever asked and whatever else was
asked alongside it. Recomputing it per analysis was therefore pure repetition —
and expensive repetition, because a candidate with no cached demand triggers a
PyTrends collection, the slowest thing in the system.

Measured on the deployed environment: three consecutive analyses of the same
product and countries took 594s, 203s and 396s. The four requested countries
collected nothing on the repeats — every second of that was candidate work being
redone, for candidates that then lost to a requested country anyway (the champion
came out BRA all three times).

Keyed by period_key rather than a TTL, because what makes a champion stale is new
demand data, and demand is collected per month.

Revision ID: 025
Revises: 024
"""

import sqlalchemy as sa
from alembic import op

revision: str = "025"
down_revision: str | None = "024"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("product_catalog", sa.Column("champion_country", sa.String(length=3), nullable=True))
    op.add_column("product_catalog", sa.Column("champion_mos", sa.Numeric(), nullable=True))
    # The period the champion was computed for. Nullable, and NULL means "never
    # computed" rather than "no champion exists" — the two are different, and
    # conflating them would make a product whose candidates genuinely cannot be
    # scored retry forever on every analysis.
    op.add_column("product_catalog", sa.Column("champion_period_key", sa.String(), nullable=True))
    op.add_column(
        "product_catalog",
        sa.Column("champion_computed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("product_catalog", "champion_computed_at")
    op.drop_column("product_catalog", "champion_period_key")
    op.drop_column("product_catalog", "champion_mos")
    op.drop_column("product_catalog", "champion_country")
