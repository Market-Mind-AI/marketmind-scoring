"""Catalog resolution pipeline (Fase 2)

Enables pg_trgm (fuzzy alias matching) and vector (semantic matching via
embeddings) extensions, adds product_catalog.embedding, and adds the GIN
trigram index used by the fuzzy-match step. See app/services/catalog_service.py.

Revision ID: 004
Revises: 003
Create Date: 2026-07-02

"""

from collections.abc import Sequence

from alembic import op

revision: str = "004"
down_revision: str | None = "003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# A coluna é criada em SQL puro, e não com `pgvector.sqlalchemy.Vector`, desde
# 2026-08-25. Motivo: o Alembic importa TODOS os arquivos de versão a cada
# `upgrade`, então este import obrigava o projeto a carregar a dependência
# `pgvector` para sempre — mesmo depois da migration 027, que remove a coluna e
# a extensão. O DDL emitido é idêntico; só deixou de precisar da biblioteca.
_EMBEDDING_DIMENSIONS = 1536


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.execute(f"ALTER TABLE product_catalog ADD COLUMN embedding vector({_EMBEDDING_DIMENSIONS})")

    op.create_index(
        "ix_product_aliases_alias_normalized_trgm",
        "product_aliases",
        ["alias_normalized"],
        postgresql_using="gin",
        postgresql_ops={"alias_normalized": "gin_trgm_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_product_aliases_alias_normalized_trgm", table_name="product_aliases")
    op.drop_column("product_catalog", "embedding")
    op.execute("DROP EXTENSION IF EXISTS vector")
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
