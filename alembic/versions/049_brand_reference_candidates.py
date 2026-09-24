"""Os candidatos a mercado de referência também são por marca.

2026-09-24, fechando a varredura. `product_catalog.reference_candidates` é a
lista de países onde o produto provavelmente é maior, e o MAIOR volume real
entre eles vira o TETO da régua de Demand Volume (Cap. 4 §4.2.1).

Com a marca fora da identidade, o produto virou um só — e essa lista passaria a
ser compartilhada. Onde o café é mais procurado não é onde a Juan Valdez é mais
procurada, e usar a régua da categoria para medir a marca desloca todos os
scores de demanda dela de uma vez.

É a última coisa por entidade que dependia da marca. As outras já foram:
vocabulário (045), campeão (047), indicadores e MOS guardados (048). Os termos
excluídos ficam no produto de propósito — eles vetam a fusão de produtos
VIZINHOS, que é pergunta do produto.

Sem marca, a lista continua onde está.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "049"
down_revision = "048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "brand_reference_candidates",
        sa.Column(
            "entity_key", postgresql.UUID(as_uuid=True), sa.ForeignKey("product_catalog.entity_key"), nullable=False
        ),
        sa.Column("brand_key", postgresql.UUID(as_uuid=True), sa.ForeignKey("brands.brand_key"), nullable=False),
        sa.Column("candidates", postgresql.JSONB(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("(now() at time zone 'utc')")),
        sa.PrimaryKeyConstraint("entity_key", "brand_key"),
    )

    # A lista que a entidade de marca tinha antes da fusão vai para a marca.
    op.execute(
        """
        INSERT INTO brand_reference_candidates (entity_key, brand_key, candidates)
        SELECT p.merged_into, p.brand_key, p.reference_candidates
        FROM product_catalog p
        WHERE p.merged_into IS NOT NULL
          AND p.brand_key IS NOT NULL
          AND p.reference_candidates IS NOT NULL
        ON CONFLICT (entity_key, brand_key) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_table("brand_reference_candidates")
