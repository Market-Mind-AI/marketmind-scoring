"""Semeia a explicação do indicador Broadband Adoption.

2026-10-22. `broadband_adoption_score` entrou na dimensão de preparação digital
na migration 044 (réguas), e o texto dele foi escrito em
`app/services/mos/explanation_seed.py` — mas a 016 já tinha rodado, então a
linha nunca chegou a `metric_explanations` / `metric_band_phrases`. O efeito
era visível: o indicador saía SEM NOME no relatório, numa linha em branco da
tabela comparativa. Mesmo padrão da 032: lê do módulo de seed para não haver
duas versões do mesmo texto.

Revision ID: 056
Revises: 055
Create Date: 2026-10-22

"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.services.mos.explanation_seed import band_phrase_rows, metric_rows

revision: str = "056"
down_revision: str | None = "055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CODE = "broadband_adoption_score"

_explanations = sa.table(
    "metric_explanations",
    sa.column("id", postgresql.UUID(as_uuid=True)),
    sa.column("metric_code", sa.String),
    sa.column("metric_kind", sa.String),
    sa.column("label", sa.String),
    sa.column("meaning", sa.Text),
    sa.column("higher_is_better", sa.Boolean),
)

_bands = sa.table(
    "metric_band_phrases",
    sa.column("id", postgresql.UUID(as_uuid=True)),
    sa.column("metric_code", sa.String),
    sa.column("band", sa.String),
    sa.column("phrase", sa.Text),
    sa.column("ordinal", sa.Integer),
)


def upgrade() -> None:
    op.execute(_explanations.delete().where(_explanations.c.metric_code == _CODE))
    op.execute(_bands.delete().where(_bands.c.metric_code == _CODE))
    op.bulk_insert(_explanations, [{"id": uuid.uuid4(), **row} for row in metric_rows() if row["metric_code"] == _CODE])
    op.bulk_insert(_bands, [{"id": uuid.uuid4(), **row} for row in band_phrase_rows() if row["metric_code"] == _CODE])


def downgrade() -> None:
    op.execute(_bands.delete().where(_bands.c.metric_code == _CODE))
    op.execute(_explanations.delete().where(_explanations.c.metric_code == _CODE))
