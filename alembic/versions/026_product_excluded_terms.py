"""Evidência negativa de identidade: product_excluded_terms.

O passo 2 da resolução (trigram sobre os aliases) decidia identidade só com
evidência positiva. Medido contra o catálogo real em 2026-08-25 — 9 entidades,
286 aliases normalizados, 102 excluded_terms utilizáveis — com o limiar 0.4 configurado:
44 dos 102 termos que o próprio catálogo declara como "isto NÃO é este produto"
resolviam para a entidade que os exclui (43,1%), e gravavam alias exato
permanente. `fritadeira de óleo` -> air fryer a 0.696; `öl fritteuse` -> air
fryer a 0.765.

Opostos compartilham quase toda a forma da palavra, então nenhuma medida de
semelhança os separa. O que os separa é o catálogo já ter dito, por mercado,
quais termos significam outro produto. Esta tabela põe esse dado ao alcance do
mesmo `similarity()` que decide o passo 2.

Com o veto: 44/102 -> 0/102, sem perder nenhum positivo (284/284); numa consulta inédita o recall vai de 94,7% para 93,0%.

Revision ID: 026
Revises: 025
"""

import sqlalchemy as sa
from alembic import op

revision: str = "026"
down_revision: str | None = "025"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "product_excluded_terms",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("entity_key", sa.UUID(as_uuid=True), sa.ForeignKey("product_catalog.entity_key"), nullable=False),
        # Mesma normalização dos aliases, para o pg_trgm comparar na mesma régua.
        sa.Column("term_normalized", sa.String(), nullable=False),
        sa.Column("term", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        # Por entidade, não global: duas entidades podem excluir o mesmo termo.
        sa.UniqueConstraint("entity_key", "term_normalized", name="uq_product_excluded_terms_entity_term"),
    )
    op.create_index("ix_product_excluded_terms_entity_key", "product_excluded_terms", ["entity_key"])
    # A extensão pg_trgm já vem da migration 004.
    op.execute(
        "CREATE INDEX ix_product_excluded_terms_term_normalized_trgm "
        "ON product_excluded_terms USING gin (term_normalized gin_trgm_ops)"
    )


def downgrade() -> None:
    op.drop_index("ix_product_excluded_terms_term_normalized_trgm", table_name="product_excluded_terms")
    op.drop_index("ix_product_excluded_terms_entity_key", table_name="product_excluded_terms")
    op.drop_table("product_excluded_terms")
