"""A marca sai da identidade do produto e vira vínculo da análise.

2026-09-24. A regra passa a ser: UM produto no catálogo — `coffee` — e a marca
registrada na ANÁLISE, com rastreabilidade de quem foi analisado com marca e
quem só com o produto.

O QUE ISSO CONSERTA. Até hoje a marca particionava a identidade: `Juan Valdez
coffee` era uma entidade, `coffee` outra, cada uma com o próprio vocabulário.
Medido em 24/09/2026, três análises do mesmo café geraram coleta em BOL, BRA e
COL DUAS VEZES — uma por entidade — porque o mesmo produto existia em dois
lugares e cada lugar comprava o próprio vocabulário na DataForSEO. E o
resolvedor ainda trocava os dois: pedir `coffee` devolvia a entidade da marca e
pedir `Juan Valdez coffee` devolvia a genérica, cada acerto gravando um alias
que tornava o erro permanente.

POR QUE O VOCABULÁRIO NÃO PODE IR PARA O PRODUTO. A partição nasceu de uma
medição de 27/08/2026: sem ela, `single malt Scotch whisky` + Cardhu e +
Glenfiddich caíam na mesma entidade e a segunda marca herdava o vocabulário e a
demanda da primeira. Juntar tudo no produto repetiria esse estrago.

Então o vocabulário passa a ter dono explícito: (produto, marca, país). Sem
marca, continua onde está — `product_catalog.localized_markets` —, que é o
vocabulário do produto. Com marca, vem desta tabela. "Juan Valdez coffee" e
"coffee" nunca medem a mesma coisa, porque a busca no Google Ads é outra.

A tabela guarda o mercado inteiro como JSON, no mesmo formato de
`localized_markets`, para que os dois caminhos sejam intercambiáveis para quem
lê: `{primary_name, formal_name, search_terms, excluded_terms, search_language,
confidence}`.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "045"
down_revision = "044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "brand_markets",
        sa.Column("entity_key", postgresql.UUID(as_uuid=True), sa.ForeignKey("product_catalog.entity_key"), nullable=False),
        sa.Column("brand_key", postgresql.UUID(as_uuid=True), sa.ForeignKey("brands.brand_key"), nullable=False),
        sa.Column("country", sa.String(length=3), nullable=False),
        sa.Column("market", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("(now() at time zone 'utc')")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("(now() at time zone 'utc')")),
        sa.PrimaryKeyConstraint("entity_key", "brand_key", "country"),
    )
    # A leitura é sempre "todos os países desta marca neste produto".
    op.create_index("ix_brand_markets_produto_marca", "brand_markets", ["entity_key", "brand_key"])

    # O vínculo da análise. `brand_key` nulo é a resposta, não a ausência de
    # uma: significa que a análise foi do PRODUTO, sem marca — e é isso que dá
    # a rastreabilidade pedida.
    op.add_column(
        "analysis_requests",
        sa.Column("brand_key", postgresql.UUID(as_uuid=True), sa.ForeignKey("brands.brand_key"), nullable=True),
    )
    # O nome como foi pedido, guardado ao lado da chave: a chave sobrevive a um
    # rename da marca, o nome preserva o que o comprador viu.
    op.add_column("analysis_requests", sa.Column("brand_name", sa.String(), nullable=True))
    op.create_index("ix_analysis_requests_brand", "analysis_requests", ["brand_key"])


def downgrade() -> None:
    op.drop_index("ix_analysis_requests_brand", table_name="analysis_requests")
    op.drop_column("analysis_requests", "brand_name")
    op.drop_column("analysis_requests", "brand_key")
    op.drop_index("ix_brand_markets_produto_marca", table_name="brand_markets")
    op.drop_table("brand_markets")
