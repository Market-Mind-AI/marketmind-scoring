"""Semeia a explicação do indicador de contexto Category Demand.

2026-08-27. `category_volume_score` é o volume de busca da CATEGORIA do
produto, medido ao lado da marca para a narrativa poder separar "não existe
mercado aqui" de "existe mercado e esta marca não está nele". Contexto puro,
nunca pontuado (decisão D2) — como `government_expenditure_score`, tem
`higher_is_better` nulo, porque um volume de categoria alto não é bom nem ruim
em si: ele é a escala contra a qual a demanda da marca deve ser lida.

As tabelas `metric_explanations` / `metric_band_phrases` são a fonte EDITÁVEL
em runtime (migration 016); `app/services/mos/explanation_seed.py` guarda só o
conteúdo inicial. Como a 016 já rodou, a linha nova precisa da sua própria
migration — e ela lê do mesmo módulo de seed, para não haver duas versões do
mesmo texto.

Revision ID: 032
Revises: 031
Create Date: 2026-08-27

"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.services.mos.explanation_seed import band_phrase_rows, metric_rows

revision: str = "032"
down_revision: str | None = "031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CODE = "category_volume_score"

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
