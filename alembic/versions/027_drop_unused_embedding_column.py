"""Remove a coluna de vetor do catálogo e a extensão que só ela usava.

O passo de embedding foi medido e retirado em 14/08/2026 (ver
docs/marketmind-transparencia-tecnica.md §1.3): nenhum limiar de distância
separa "mesmo produto" de "produto vizinho" neste catálogo. Desde então
`product_catalog.embedding` era peso morto — no banco de desenvolvimento, 54 kB
de vetores numa tabela de 320 kB, ~17% do peso da tabela em dado que nenhum
código lê. Não havia nem índice ANN (`ivfflat`/`hnsw`): a busca por cosseno
sempre foi varredura sequencial, então o passo nunca foi exercitado em escala.

Manter a coluna custava mais do que espaço: `Vector(settings.openai_embedding_dimensions)`
amarrava o schema a uma configuração de um provedor que não é mais usado para
nada, e a migration 004 e o modelo tinham que concordar sobre 1536 à mão.

O `downgrade` recria coluna e extensão, mas NÃO os vetores: eles teriam que ser
regerados por um provedor de embeddings, que o código não tem mais.

Revision ID: 027
Revises: 026
"""

from alembic import op

revision: str = "027"
down_revision: str | None = "026"
branch_labels: str | None = None
depends_on: str | None = None

_DIMENSOES = 1536  # o que a migration 004 criou


def upgrade() -> None:
    op.drop_column("product_catalog", "embedding")
    # pg_trgm continua: é o índice do passo 2. Só `vector` sai, porque a coluna
    # acima era a única coisa no banco que dependia dela.
    op.execute("DROP EXTENSION IF EXISTS vector")


def downgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(f"ALTER TABLE product_catalog ADD COLUMN embedding vector({_DIMENSOES})")
