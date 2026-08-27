"""Cadastro de marcas + a marca como PARTIÇÃO da identidade do catálogo.

2026-08-27. A unidade de identidade do catálogo era o produto GENÉRICO, e a
marca ficava acumulada como texto (`product_catalog.brands`, migration 029).
Medido no banco vivo, isso produz um erro que o usuário viu no relatório: o
prompt "cardhu scott" gerou a entidade `single malt Scotch whisky` com 24
mercados de vocabulário 100% Cardhu — o rótulo dizia a categoria e o número
media a marca. Pior que o rótulo: resolver `single malt Scotch whisky` com
`brand=Glenfiddich` devolve `method: alias` para ESSA MESMA entidade, pelo
caminho O(1). Uma análise da Glenfiddich leria os termos da Cardhu e
reportaria a demanda dela como se fosse da Glenfiddich.

A correção NÃO é voltar a concatenar `marca + nome` no texto que decide
semelhança — isso reintroduziria a fusão medida em 28ca6be, onde
`philips air purifier` casava com o alias `philips airfryer` a 0.520 porque a
marca é um token que todos os produtos daquela marca compartilham.

A marca vira PARTIÇÃO: a identidade passa a ser o par (nome genérico, marca).
Os passos 2 e 3 da resolução continuam comparando SÓ o nome genérico, mas
apenas contra entidades da mesma partição — dentro da Philips a comparação
volta a ser `air purifier` x `airfryer` = 0.278, abaixo do limiar, e as duas
seguem separadas. `brand_key IS NULL` é a partição genérica.

Três objetos:

  brands                          cadastro global. Uma marca é a mesma entidade
                                  em todos os produtos dela — a Philips da air
                                  fryer é a Philips do purificador —, então é
                                  tabela própria, não coluna repetida. É ela
                                  que vai alimentar o veto cruzado do A3:
                                  keyword relacionada que contenha o nome de
                                  OUTRA marca do cadastro não entra na demanda
                                  desta.

  product_catalog.brand_key       a partição. NULL = entidade genérica.

  product_catalog.generic_entity_key
                                  a família da entidade de marca. É o que torna
                                  possível medir a categoria junto com a marca
                                  e dizer "a Cardhu é fraca aqui, mas a busca
                                  por whisky é altíssima" (decisão D2), sem
                                  misturar os dois vocabulários numa entidade
                                  só.

`product_catalog.brands` (o JSONB da 029) fica NESTA migration, de propósito:
ele é hoje o único ponteiro de uma entidade poluída para as marcas que
grudaram nela, e é dele que o reparo do catálogo (A6) vai partir. Vira
redundante depois do reparo — a relação passa a ser derivável por
`generic_entity_key` — e sai numa migration posterior, não aqui, para não
apagar dado antes de ele ter sido usado.

A CHECK garante o invariante do modelo: entidade com marca tem família. Não
existe marca sem produto genérico por trás — o próprio LLM de identificação
sempre devolve o nome genérico junto (`product_name` sem marca, por prompt).
As linhas existentes têm `brand_key` NULL e passam.

Revision ID: 031
Revises: 030
Create Date: 2026-08-27

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "031"
down_revision: str | None = "030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "brands",
        sa.Column("brand_key", sa.UUID(as_uuid=True), primary_key=True),
        # Forma de superfície como vista pela primeira vez ("Cardhu"), para exibir.
        sa.Column("name", sa.String(), nullable=False),
        # Mesma normalização dos aliases (app.utils.text.normalize_alias), para
        # que "Cardhu", "CARDHU" e "cardhu" sejam a mesma marca.
        sa.Column("name_normalized", sa.String(), nullable=False),
        sa.Column("first_seen_source", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("name_normalized", name="uq_brands_name_normalized"),
        # Mesma guarda da migration 028: texto que normaliza para vazio não é
        # marca, e gravá-lo cria um ímã que casa com qualquer consulta sem
        # forma normalizada.
        sa.CheckConstraint("length(trim(name_normalized)) > 0", name="ck_brands_name_normalized_not_empty"),
    )

    op.add_column(
        "product_catalog",
        sa.Column("brand_key", sa.UUID(as_uuid=True), sa.ForeignKey("brands.brand_key"), nullable=True),
    )
    op.add_column(
        "product_catalog",
        sa.Column(
            "generic_entity_key",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("product_catalog.entity_key"),
            nullable=True,
        ),
    )
    op.create_index("ix_product_catalog_brand_key", "product_catalog", ["brand_key"])
    op.create_index("ix_product_catalog_generic_entity_key", "product_catalog", ["generic_entity_key"])
    op.create_check_constraint(
        "ck_product_catalog_brand_has_generic",
        "product_catalog",
        "brand_key IS NULL OR generic_entity_key IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint("ck_product_catalog_brand_has_generic", "product_catalog", type_="check")
    op.drop_index("ix_product_catalog_generic_entity_key", table_name="product_catalog")
    op.drop_index("ix_product_catalog_brand_key", table_name="product_catalog")
    op.drop_column("product_catalog", "generic_entity_key")
    op.drop_column("product_catalog", "brand_key")
    op.drop_table("brands")
