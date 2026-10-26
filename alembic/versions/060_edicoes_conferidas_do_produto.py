"""As edições que usuários fizeram em produtos do catálogo, conferidas.

2026-10-26. A tela de revisão deixa o usuário mudar categoria, faixa etária e
preço, e o /analyze gravava o que chegasse por cima do catálogo — que é
compartilhado: a mudança de uma pessoa mudava a nota de todas as análises
seguintes do produto, sem registro de quem mudou nem do valor anterior. Agora
o modelo confere cada edição contra o produto, e cada decisão fica aqui.

Revision ID: 060
Revises: 059
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "060"
down_revision = "059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "product_edits",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("entity_key", UUID(as_uuid=True), sa.ForeignKey("product_catalog.entity_key"), nullable=False),
        sa.Column("field", sa.String(), nullable=False),
        sa.Column("old_value", sa.String(), nullable=True),
        sa.Column("new_value", sa.String(), nullable=True),
        sa.Column("accepted", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_product_edits_entity_key", "product_edits", ["entity_key"])


def downgrade() -> None:
    op.drop_index("ix_product_edits_entity_key", table_name="product_edits")
    op.drop_table("product_edits")
