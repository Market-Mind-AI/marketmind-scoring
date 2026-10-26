"""O preço estimado do produto, guardado com ele.

2026-10-26. A identificação passou a devolver o produto como o catálogo o
guarda quando ele já existe (léxico ou candidato reconhecido), sem adivinhar
nada de novo. O preço nunca tinha sido gravado, então a tela de revisão abria
com ele vazio. Continua sendo só contexto: nunca entra na nota.

NULO é "identificado antes desta migration".

Revision ID: 059
Revises: 058
"""

import sqlalchemy as sa

from alembic import op

revision = "059"
down_revision = "058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("product_catalog", sa.Column("estimated_price", sa.Numeric(), nullable=True))
    op.add_column("product_catalog", sa.Column("price_currency", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("product_catalog", "price_currency")
    op.drop_column("product_catalog", "estimated_price")
