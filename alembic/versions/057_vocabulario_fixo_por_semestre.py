"""Quando o vocabulário de busca de cada produto foi fixado.

2026-10-23. Cada identificação devolve uma lista de termos um pouco diferente,
e a análise somava os novos ao vocabulário do produto. As palavras
relacionadas do Google Ads são guardadas pela lista exata de termos usada como
semente, então a lista nova não encontrava nada guardado e o Google Ads era
pago de novo em todos os países: 0,90 USD na segunda análise do jamón ibérico,
minutos depois da primeira.

Com esta data, dentro de `search_terms_ttl_days` (um semestre) uma nova
identificação só acrescenta países que faltam. O preenchimento inicial usa
`created_at`: é quando o vocabulário de cada produto foi escrito pela primeira
vez, e assim o que já existe passa a valer desde já, sem uma rodada extra de
custo para cada produto.

Revision ID: 057
Revises: 056
"""

import sqlalchemy as sa

from alembic import op

revision = "057"
down_revision = "056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("product_catalog", sa.Column("vocabulary_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE product_catalog SET vocabulary_at = created_at WHERE localized_markets IS NOT NULL")


def downgrade() -> None:
    op.drop_column("product_catalog", "vocabulary_at")
