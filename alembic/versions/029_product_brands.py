"""Acumula as marcas vistas para cada produto do catálogo.

A identidade do catálogo é o PRODUTO GENÉRICO, não o SKU: `air fryer` é uma
entidade, e Philips, Ninja e Cosori são marcas dela. O prompt de visão já diz
isso ao modelo ("Put the brand only in `brand`"), mas o código concatenava
marca e nome antes de resolver — e a marca, sendo um token que TODOS os
produtos daquela marca compartilham, aproximava artificialmente coisas que só
tinham a marca em comum. Medido no catálogo real: `air purifier` x `air fryer`
= 0.278, longe do limiar; `philips air purifier` x o alias `philips airfryer`
= 0.520, e a fusão acontecia. Em 11 de 12 casos medidos a marca não mudava
nada; no 12º mudou para pior.

A marca sai da decisão de identidade e passa a ser acumulada aqui — uma marca
nova vista para um produto é acrescentada, nunca substitui as anteriores. Isso
preserva o que a concatenação tinha de bom (não perder a marca) sem o que ela
tinha de ruim (decidir identidade com um token compartilhado).

JSONB e não tabela própria, ao contrário de `product_excluded_terms`: aquela
precisa ser CONSULTÁVEL por `similarity()` com índice GIN, porque participa do
veto do passo 2. Marca é lida junto com a entidade e nunca pesquisada sozinha —
mesma natureza de `localized_markets` e `reference_candidates`.

Revision ID: 029
Revises: 028
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "029"
down_revision: str | None = "028"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("product_catalog", sa.Column("brands", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("product_catalog", "brands")
