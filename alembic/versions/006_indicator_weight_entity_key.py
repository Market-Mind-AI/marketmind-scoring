"""Indicator weight entity_key (Fase 4)

indicator_weights was product-agnostic in the original Fase 0 schema, but
the Demand dimension's weight (Cap. 5 §5.5 correction — see
marketmind_mos_engine) is calibrated per-product against the reference
country population, not shared across products like the other 4 macro
dimensions. Adds a nullable entity_key (NULL = macro dimension weight,
shared; set = Demand weight for that specific product) and swaps the single
unique index for the same two-partial-index pattern used everywhere else in
this schema (coverage_index, monthly_snapshots, job_tasks, ...) since NULL
never collides with itself in a Postgres unique constraint.

Revision ID: 006
Revises: 005
Create Date: 2026-07-03

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "006"
down_revision: str | None = "005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "indicator_weights",
        sa.Column(
            "entity_key", postgresql.UUID(as_uuid=True), sa.ForeignKey("product_catalog.entity_key"), nullable=True
        ),
    )

    op.drop_index("ix_indicator_weights_lookup", table_name="indicator_weights")

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


def downgrade() -> None:
    op.drop_index("ix_indicator_weights_lookup_country_level", table_name="indicator_weights")
    op.drop_index("ix_indicator_weights_lookup_with_entity", table_name="indicator_weights")
    op.create_index("ix_indicator_weights_lookup", "indicator_weights", ["indicator_code", "period_key"], unique=True)
    op.drop_column("indicator_weights", "entity_key")
