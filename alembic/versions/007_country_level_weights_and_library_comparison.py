"""Country-level weights + library comparison fields (Fase 4 redesign)

Reverses migration 006: the Demand dimension's weight is no longer
per-product. All 5 dimension weights (Cap. 5 §5.5) are now calculated and
stored per country/period only, shared across every product — see memory
marketmind_mos_engine (2026-07-03 correction). A product-specific Demand
weight broke cross-product comparability (two products' MOS were combined
with different formulas) and required live PyTrends collection on every new
product's first weight calibration, which is slow and rate-limit-fragile.
Demand's country-level value is now an aggregate (average) across whatever
products already have data for that country, grown organically through
normal usage instead of a synchronous per-product collection step.

Also adds `library_topsis_score`/`library_mos` to mos_results: a parallel
computation using the classical (textbook) Entropy Weight + TOPSIS from the
`pymcdm` library, for manual comparison against our corrected implementation
(fixed ideal/anti-ideal, no per-query renormalization) — see
app/services/mos/library_reference.py. Not used in production MOS
selection, purely a validation artifact.

Revision ID: 007
Revises: 006
Create Date: 2026-07-03

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "007"
down_revision: str | None = "006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_indicator_weights_lookup_country_level", table_name="indicator_weights")
    op.drop_index("ix_indicator_weights_lookup_with_entity", table_name="indicator_weights")
    op.drop_column("indicator_weights", "entity_key")
    op.create_index("ix_indicator_weights_lookup", "indicator_weights", ["indicator_code", "period_key"], unique=True)

    op.add_column("mos_results", sa.Column("library_topsis_score", sa.Numeric(), nullable=True))
    op.add_column("mos_results", sa.Column("library_mos", sa.Numeric(), nullable=True))


def downgrade() -> None:
    op.drop_column("mos_results", "library_mos")
    op.drop_column("mos_results", "library_topsis_score")

    op.drop_index("ix_indicator_weights_lookup", table_name="indicator_weights")
    op.add_column(
        "indicator_weights",
        sa.Column(
            "entity_key",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("product_catalog.entity_key"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_indicator_weights_lookup_with_entity",
        "indicator_weights",
        ["indicator_code", "entity_key", "period_key"],
        unique=True,
        postgresql_where=sa.text("entity_key IS NOT NULL"),
    )
    op.create_index(
        "ix_indicator_weights_lookup_country_level",
        "indicator_weights",
        ["indicator_code", "period_key"],
        unique=True,
        postgresql_where=sa.text("entity_key IS NULL"),
    )
