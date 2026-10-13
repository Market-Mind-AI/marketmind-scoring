"""Busca semântica do catálogo: um vetor por alias, com índice HNSW.

2026-10-13. A extensão `vector` volta (a 027 a removeu), com outro papel que o
da coluna `product_catalog.embedding` da 004: o vetor não aceita match, só
ESCOLHE quais entidades o LLM do passo 3 vai julgar. A medição de 14/08/2026
(nenhum limiar separa `freidora de aire` de `freidora de aceite`) continua
proibindo o vetor de decidir; ela não diz nada contra usá-lo para recuperar.

POR QUE NO ALIAS E NÃO NA ENTIDADE. Uma entidade tem dezenas de termos em
idiomas diferentes. Um vetor só para tudo isso é a média de `air fryer`,
`freidora de aire` e `ノンフライヤー` — um ponto que não fica perto de nenhum
deles. Um vetor por termo deixa `heißluftfritteuse` achar o termo alemão da
entidade, e a entidade sai pela melhor distância entre os seus termos. Termo
excluído não entra: ele é evidência de que o texto NÃO é a entidade.

POR QUE HNSW E NÃO IVFFLAT. O IVFFlat aprende os centróides com os dados que
existem na hora do CREATE INDEX e precisa ser reconstruído quando a
distribuição muda; o catálogo cresce uma entidade por vez, e o índice criado
sobre uma tabela vazia seria inútil. O HNSW insere incrementalmente.
`m = 16` e `ef_construction = 64` são os defaults do pgvector; o parâmetro
que se ajusta é o `ef_search`, na consulta (ver settings).

`embedding_model` registra quem gerou o vetor. Vetores de modelos diferentes
não são comparáveis, e trocar `VOYAGE_EMBEDDING_MODEL` sem essa coluna
misturaria os dois espaços em silêncio; com ela, o backfill sabe o que refazer.

Revision ID: 052
Revises: 051
"""

from alembic import op

revision = "052"
down_revision = "051"
branch_labels = None
depends_on = None

# Fixo aqui, como a 004 fazia: migration não lê settings. Tem que bater com
# VOYAGE_EMBEDDING_DIMENSIONS.
_DIMENSOES = 1024


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(f"ALTER TABLE product_aliases ADD COLUMN embedding vector({_DIMENSOES})")
    op.execute("ALTER TABLE product_aliases ADD COLUMN embedding_model text")
    op.execute(
        "CREATE INDEX ix_product_aliases_embedding_hnsw ON product_aliases "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_product_aliases_embedding_hnsw")
    op.execute("ALTER TABLE product_aliases DROP COLUMN IF EXISTS embedding_model")
    op.execute("ALTER TABLE product_aliases DROP COLUMN IF EXISTS embedding")
    op.execute("DROP EXTENSION IF EXISTS vector")
