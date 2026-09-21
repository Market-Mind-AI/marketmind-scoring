"""Tira do catálogo o enriquecimento por google_mid, que não tem mais produtor.

2026-09-21. `google_mid`/`google_type` vinham do endpoint `suggestions` do
serviço de demanda, que sumiu quando ele deixou o Google Trends e passou a
coletar volume do Google Ads. Quem preenchia essas colunas lia a tabela
`suggestions` daquele serviço, já removida lá — ou seja, o enriquecimento
falhava ao ser chamado, e nada mais escrevia aqui.

Nenhuma linha tinha valor, então não há dado a preservar. A identidade do
produto nunca dependeu disso: ela é texto normalizado mais trigram, e o `mid`
sempre foi enriquecimento opcional que não podia bloquear nada.

`merged_into` FICA. Parece do mesmo conjunto (era o que a fusão por `mid`
escrevia), mas continua vivo por outro caminho: é como a separação de marca
aposenta a entidade poluída, e três consultas de resolução filtram por ele.

Revision ID: 035
Revises: 034
Create Date: 2026-09-21

"""

import sqlalchemy as sa
from alembic import op

revision = "035"
down_revision = "034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_product_catalog_google_mid", table_name="product_catalog")
    op.drop_column("product_catalog", "google_mid")
    op.drop_column("product_catalog", "google_type")


def downgrade() -> None:
    op.add_column("product_catalog", sa.Column("google_type", sa.String(), nullable=True))
    op.add_column("product_catalog", sa.Column("google_mid", sa.String(), nullable=True))
    op.create_index("ix_product_catalog_google_mid", "product_catalog", ["google_mid"])
