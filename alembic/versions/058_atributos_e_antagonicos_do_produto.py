"""O que faz deste produto ESTE produto, e o que se confunde com ele.

2026-10-26. A identificação devolvia só nome genérico, marca e categoria, e o
nome genérico perdia o modelo e o tipo: "Snapmaker U1" virava "3D printer", e
o enriquecimento do /analyze, que só recebe o nome, gerava termos da marca
inteira — outros modelos, outros tipos de impressora.

A mesma chamada de identificação passa a devolver os atributos que definem o
produto e os produtos antagônicos (os que se confundem com ele e são outro).
Ficam aqui porque o enriquecimento roda depois, em outra requisição, e só tem
a entidade nas mãos. Os antagônicos por idioma continuam em
`localized_markets` (migration 053); estes são a lista de onde eles partem.

NULO é "identificado antes desta migration": o enriquecimento segue como era.

Revision ID: 058
Revises: 057
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "058"
down_revision = "057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("product_catalog", sa.Column("defining_attributes", JSONB, nullable=True))
    op.add_column("product_catalog", sa.Column("antagonist_products", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("product_catalog", "antagonist_products")
    op.drop_column("product_catalog", "defining_attributes")
