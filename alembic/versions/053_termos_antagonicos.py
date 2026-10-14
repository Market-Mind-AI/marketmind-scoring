"""Produto antagônico: evidência negativa com vetor, para o veto semântico.

2026-10-13. O passo 3a recupera por proximidade de significado, e a medição de
14/08 continua de pé: `fritadeira a ar` x `fritadeira a óleo` = 0,065, mais
perto que `fritadeira a ar` x `air fryer` = 0,106. Nenhum limiar absoluto
separa os dois. O que separa é a comparação RELATIVA que já funcionava no
trigram (passo 2, 44/102 fusões -> 0): o texto está mais perto do que a
entidade diz que É, ou do produto que ela diz que se confunde com ela? Medido
com 16 consultas inéditas em 8 idiomas: 16/16 com voyage-4-large, 15/15 + 1
ambígua com voyage-4.

POR QUE NÃO EMBEDAR OS `excluded_terms` QUE JÁ EXISTEM. Eles misturam duas
coisas: produto antagônico (`fritadeira a óleo`) e palavra de USO (`na
airfryer`, `receita`), que servem para filtrar volume de busca. A segunda
categoria fica perto do próprio produto no espaço vetorial — `receita de
frango na airfryer` é sobre air fryer — e vetaria buscas legítimas dele.
Daí `kind`: só as linhas `antagonist` recebem vetor.

Mesma tabela e não uma nova porque o antagônico TAMBÉM é evidência negativa
para o trigram: `fritadeira industrial a óleo` precisa vetar o passo 2 tanto
quanto o 3a.

Revision ID: 053
Revises: 052
"""

from alembic import op

revision = "053"
down_revision = "052"
branch_labels = None
depends_on = None

_DIMENSOES = 1024  # igual à 052; tem que bater com VOYAGE_EMBEDDING_DIMENSIONS


def upgrade() -> None:
    op.execute("ALTER TABLE product_excluded_terms ADD COLUMN kind text NOT NULL DEFAULT 'excluded'")
    op.execute(
        "ALTER TABLE product_excluded_terms ADD CONSTRAINT ck_product_excluded_terms_kind "
        "CHECK (kind IN ('excluded', 'antagonist'))"
    )
    op.execute(f"ALTER TABLE product_excluded_terms ADD COLUMN embedding vector({_DIMENSOES})")
    op.execute("ALTER TABLE product_excluded_terms ADD COLUMN embedding_model text")
    # Sem índice HNSW: o veto lê os antagônicos de POUCAS entidades conhecidas
    # (WHERE entity_key IN ...), dezenas de linhas, e a varredura exata é mais
    # barata e mais certa que um índice aproximado. O índice por entidade já
    # existe (ix_product_excluded_terms_entity_key).


def downgrade() -> None:
    op.execute("ALTER TABLE product_excluded_terms DROP COLUMN IF EXISTS embedding_model")
    op.execute("ALTER TABLE product_excluded_terms DROP COLUMN IF EXISTS embedding")
    op.execute("ALTER TABLE product_excluded_terms DROP CONSTRAINT IF EXISTS ck_product_excluded_terms_kind")
    op.execute("ALTER TABLE product_excluded_terms DROP COLUMN IF EXISTS kind")
