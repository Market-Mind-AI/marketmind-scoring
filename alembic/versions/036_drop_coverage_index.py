"""Tira a coverage_index, que era uma segunda cópia do frescor alheio.

2026-09-21. A tabela guardava, por (fonte, produto, país, indicador, período),
quando este serviço tinha coletado aquilo — para decidir o que ainda faltava
antes de disparar coleta nova. O dado, porém, é dos quatro serviços de coleta,
e cada um deles já registra quando olhou pela última vez e responde isso em
`GET /freshness`, em lote, por país e indicador.

Duas cópias do mesmo fato, e elas discordavam. Um gap de demanda só é criado
porque o serviço de PyTrends acabou de dizer que não tem aqueles termos — e o
`_process_gap` ainda consultava esta tabela e PULAVA a coleta se ela dissesse
"fresco". A cópia desatualizada ganhava da fonte que tinha acabado de
responder, e a coleta legítima era descartada em silêncio.

Nada a preservar: a tabela não é dado de negócio, é cache de decisão. O que ela
respondia, as fontes respondem melhor — a `last_checked_at` de cada uma é agora
o único registro.

`jobs` e `job_tasks` FICAM. Parecem do mesmo conjunto, mas não são frescor de
ninguém: são o registro deste serviço sobre o que ele despachou e o que voltou,
e é dele que sai a distinção entre "a fonte caiu, vale insistir" e "a fonte não
tem esse dado" — que é o que decide se uma análise insiste ou desiste.

O downgrade recria a tabela vazia. Não há como repovoá-la, e não é preciso: um
índice de cobertura vazio significa "nada coletado", que faz o serviço perguntar
às fontes — que é exatamente o comportamento novo.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "036"
down_revision = "035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_coverage_index_is_closed", table_name="coverage_index")
    op.drop_index("ix_coverage_index_lookup_country_level", table_name="coverage_index")
    op.drop_index("ix_coverage_index_lookup_with_entity", table_name="coverage_index")
    op.drop_table("coverage_index")


def downgrade() -> None:
    op.create_table(
        "coverage_index",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column(
            "entity_key",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("product_catalog.entity_key"),
            nullable=True,
        ),
        sa.Column("country", sa.String(length=3), nullable=False),
        sa.Column("indicator_code", sa.String(), nullable=False),
        sa.Column("period_key", sa.String(), nullable=False),
        sa.Column("is_closed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("last_fetched_at", sa.DateTime(), nullable=False),
        sa.Column("ttl_days", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_coverage_index_lookup_with_entity",
        "coverage_index",
        ["source", "entity_key", "country", "indicator_code", "period_key"],
        unique=True,
        postgresql_where=sa.text("entity_key IS NOT NULL"),
    )
    op.create_index(
        "ix_coverage_index_lookup_country_level",
        "coverage_index",
        ["source", "country", "indicator_code", "period_key"],
        unique=True,
        postgresql_where=sa.text("entity_key IS NULL"),
    )
    op.create_index("ix_coverage_index_is_closed", "coverage_index", ["is_closed"])
