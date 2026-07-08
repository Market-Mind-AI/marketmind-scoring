"""Macro Demand Structure dimension + LLM context fields (Fase 4 extension)

Adds the 6th MOS dimension (Macro Demand Structure, GDP = C+I+G+NX identity)
and the "contexto para LLM em todos os scores, métricas e MOS" feature
(user request 2026-07-08) — both purely additive, no existing column is
renamed or dropped. New indicator_code values themselves (consumer_spending_
score, investment_score, net_exports_score, trade_openness_score,
government_expenditure_score, real_estate_growth_score) don't need schema
changes — indicator_values.indicator_code is already a free-text String, not
an enum constrained at the DB level; only the two new descriptive columns
below and the mos_results interpretation column are structural.

business_context/interpretation_band (indicator_values) and
mos_interpretation (mos_results) are deterministic, LLM-context-only
metadata (see app/services/mos/interpretation.py) — nullable and never
backfilled for historical rows, consistent with how this schema has always
treated purely descriptive additions (no migration in this project has ever
backfilled `notes` either).

Revision ID: 008
Revises: 007
Create Date: 2026-07-08

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "008"
down_revision: str | None = "007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("indicator_values", sa.Column("business_context", sa.Text(), nullable=True))
    op.add_column("indicator_values", sa.Column("interpretation_band", sa.String(), nullable=True))
    op.add_column("mos_results", sa.Column("mos_interpretation", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("mos_results", "mos_interpretation")
    op.drop_column("indicator_values", "interpretation_band")
    op.drop_column("indicator_values", "business_context")
