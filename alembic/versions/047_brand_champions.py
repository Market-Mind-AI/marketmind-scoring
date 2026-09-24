"""O campeão também é por marca, não por produto.

2026-09-24, fechando o que a 045 começou. Com a marca fora da identidade, o
produto virou um só — e o campeão, que mora nele, passaria a ser compartilhado
por todas as marcas dele. Seria a mesma contaminação de 27/08/2026 entrando por
outra porta: o melhor mercado da Cardhu viraria o melhor mercado do whisky, e
depois o da Glenfiddich.

Pior que errado, é AUTOCONFIRMANTE. Campeão conhecido é o sinal de que não há o
que recolher — `champion_cached` faz a análise pular a coleta dos mercados de
referência, que é o passo mais caro do sistema. Um campeão herdado de outra
marca nunca seria corrigido, porque o próprio fato de existir impede a medição
que o desmentiria.

Tabela separada em vez de coluna com marca nula, pela mesma razão de
`brand_markets`: nulo não serve como parte de chave primária no Postgres, e um
UUID-sentinela para "sem marca" seria uma mentira guardada. Sem marca, o
campeão continua onde sempre esteve, em `product_catalog.champion_*`.

`country` nulo com `period_key` preenchido continua significando "nada
pontuável neste período" — é o que impede a análise seguinte de tentar de novo.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "047"
down_revision = "046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "brand_champions",
        sa.Column(
            "entity_key", postgresql.UUID(as_uuid=True), sa.ForeignKey("product_catalog.entity_key"), nullable=False
        ),
        sa.Column("brand_key", postgresql.UUID(as_uuid=True), sa.ForeignKey("brands.brand_key"), nullable=False),
        sa.Column("period_key", sa.String(), nullable=False),
        sa.Column("country", sa.String(length=3), nullable=True),
        sa.Column("mos", sa.Numeric(), nullable=True),
        sa.Column("computed_at", sa.DateTime(), nullable=False, server_default=sa.text("(now() at time zone 'utc')")),
        sa.PrimaryKeyConstraint("entity_key", "brand_key", "period_key"),
    )


def downgrade() -> None:
    op.drop_table("brand_champions")
