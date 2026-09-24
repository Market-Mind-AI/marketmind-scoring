"""O indicador e o MOS guardados também são por marca.

2026-09-24, fechando o buraco que a 045 abriu e que só apareceu medindo.

Com a marca fora da identidade o produto virou um só, e `indicator_values` é
chaveado por (entity_key, country, indicator_code, período) — sem marca. Então
a análise da marca e a do produto gravam NA MESMA LINHA e uma sobrescreve a
outra.

Medido em 24/09/2026, e o sintoma enganava: os MOS saíram diferentes (38,44 vs
37,82 no Brasil) e pareciam prova de que a separação funcionava. Não era. As
duas análises leram a linha que a outra tinha acabado de escrever — a diferença
era ordem de execução. `trend_score` valia 3.557.340 nas quatro análises, com e
sem marca.

`brand_key` entra nos índices únicos, e nulo continua significando "do
produto". Postgres trata nulos como distintos num índice único, então um índice
só com a coluna anulável deixaria passar duplicata do caso sem marca — por isso
são três índices parciais, um por situação, cada um com a condição explícita.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "048"
down_revision = "047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _indicator_values()
    _mos_results()


def _mos_results() -> None:
    """`mos_results` tinha o mesmo defeito, pela mesma razão: único em
    (entity_key, country, período), sem marca. O MOS da marca sobrescrevia o do
    produto."""
    op.add_column(
        "mos_results",
        sa.Column("brand_key", postgresql.UUID(as_uuid=True), sa.ForeignKey("brands.brand_key"), nullable=True),
    )
    op.drop_index("ix_mos_results_lookup", table_name="mos_results")
    op.create_index(
        "ix_mos_results_lookup_produto",
        "mos_results",
        ["entity_key", "country", "period_type", "period_key"],
        unique=True,
        postgresql_where=sa.text("brand_key IS NULL"),
    )
    op.create_index(
        "ix_mos_results_lookup_marca",
        "mos_results",
        ["entity_key", "brand_key", "country", "period_type", "period_key"],
        unique=True,
        postgresql_where=sa.text("brand_key IS NOT NULL"),
    )


def _indicator_values() -> None:
    op.add_column(
        "indicator_values",
        sa.Column("brand_key", postgresql.UUID(as_uuid=True), sa.ForeignKey("brands.brand_key"), nullable=True),
    )
    op.drop_index("ix_indicator_values_lookup_with_entity", table_name="indicator_values")
    op.create_index(
        "ix_indicator_values_lookup_produto",
        "indicator_values",
        ["entity_key", "country", "indicator_code", "period_type", "period_key"],
        unique=True,
        postgresql_where=sa.text("entity_key IS NOT NULL AND brand_key IS NULL"),
    )
    op.create_index(
        "ix_indicator_values_lookup_marca",
        "indicator_values",
        ["entity_key", "brand_key", "country", "indicator_code", "period_type", "period_key"],
        unique=True,
        postgresql_where=sa.text("entity_key IS NOT NULL AND brand_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.execute("DELETE FROM mos_results WHERE brand_key IS NOT NULL")
    op.drop_index("ix_mos_results_lookup_marca", table_name="mos_results")
    op.drop_index("ix_mos_results_lookup_produto", table_name="mos_results")
    op.create_index(
        "ix_mos_results_lookup", "mos_results", ["entity_key", "country", "period_type", "period_key"], unique=True
    )
    op.drop_column("mos_results", "brand_key")

    # As linhas de marca saem: sem a coluna elas colidiriam com as do produto,
    # que é exatamente o estado que esta migration existe para acabar.
    op.execute("DELETE FROM indicator_values WHERE brand_key IS NOT NULL")
    op.drop_index("ix_indicator_values_lookup_marca", table_name="indicator_values")
    op.drop_index("ix_indicator_values_lookup_produto", table_name="indicator_values")
    op.create_index(
        "ix_indicator_values_lookup_with_entity",
        "indicator_values",
        ["entity_key", "country", "indicator_code", "period_type", "period_key"],
        unique=True,
        postgresql_where=sa.text("entity_key IS NOT NULL"),
    )
    op.drop_column("indicator_values", "brand_key")
