"""Metric explanation layer, parametrized in the DB

2026-07-22 (user request: "todos os scores devem ter sua explicação sobre seu
número... frases por banda editáveis... tudo parametrizado no banco"). Two new
tables hold the EDITABLE explanation content:

  - metric_explanations: one row per metric (each Cap. 4 indicator, each Cap. 5
    dimension, and the MOS) — label, meaning sentence, and direction flag.
  - metric_band_phrases: one phrase per (metric, band) — the reading of what
    that band means for that metric (Very Low..Very High; the MOS uses its six
    §5.8 opportunity bands).

And an `explanation` column is added to indicator_values and mos_results: the
deterministic per-score sentence (label + band phrase + concrete detail) built
at compute time from the tables above and fed to the final LLM (which narrates
it, never recomputes — Cap. 4 §4.0). Seeded from
app/services/mos/explanation_seed.py (meanings imported from interpretation.py,
their canonical home). The DB rows are the editable runtime source.

Revision ID: 016
Revises: 015
Create Date: 2026-07-22

"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.services.mos.explanation_seed import band_phrase_rows, metric_rows

revision: str = "016"
down_revision: str | None = "015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_metric_explanations = sa.table(
    "metric_explanations",
    sa.column("id", postgresql.UUID(as_uuid=True)),
    sa.column("metric_code", sa.String),
    sa.column("metric_kind", sa.String),
    sa.column("label", sa.String),
    sa.column("meaning", sa.Text),
    sa.column("higher_is_better", sa.Boolean),
)

_metric_band_phrases = sa.table(
    "metric_band_phrases",
    sa.column("id", postgresql.UUID(as_uuid=True)),
    sa.column("metric_code", sa.String),
    sa.column("band", sa.String),
    sa.column("phrase", sa.Text),
    sa.column("ordinal", sa.Integer),
)


def upgrade() -> None:
    op.create_table(
        "metric_explanations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("metric_code", sa.String(), nullable=False),
        sa.Column("metric_kind", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("meaning", sa.Text(), nullable=False),
        sa.Column("higher_is_better", sa.Boolean(), nullable=True),
    )
    op.create_index("ix_metric_explanations_code", "metric_explanations", ["metric_code"], unique=True)

    op.create_table(
        "metric_band_phrases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("metric_code", sa.String(), nullable=False),
        sa.Column("band", sa.String(), nullable=False),
        sa.Column("phrase", sa.Text(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["metric_code"], ["metric_explanations.metric_code"], ondelete="CASCADE"),
    )
    op.create_index("ix_metric_band_phrases_code_band", "metric_band_phrases", ["metric_code", "band"], unique=True)

    op.add_column("indicator_values", sa.Column("explanation", sa.Text(), nullable=True))
    op.add_column("mos_results", sa.Column("explanation", sa.Text(), nullable=True))

    op.bulk_insert(_metric_explanations, [{"id": uuid.uuid4(), **row} for row in metric_rows()])
    op.bulk_insert(_metric_band_phrases, [{"id": uuid.uuid4(), **row} for row in band_phrase_rows()])


def downgrade() -> None:
    op.drop_column("mos_results", "explanation")
    op.drop_column("indicator_values", "explanation")
    op.drop_index("ix_metric_band_phrases_code_band", table_name="metric_band_phrases")
    op.drop_table("metric_band_phrases")
    op.drop_index("ix_metric_explanations_code", table_name="metric_explanations")
    op.drop_table("metric_explanations")
