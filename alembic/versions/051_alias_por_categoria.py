"""Nome igual em categorias diferentes deixa de ser o mesmo produto.

2026-09-24. `alias_normalized` era único no mundo: dois produtos não podiam se
chamar `jaguar`. Medido no mesmo dia, com catálogo limpo: `jaguar` em Automotive
e `jaguar` em Wildlife caíam na MESMA entidade pelo passo 1 — o caminho O(1), o
mais barato, e por isso o único que nada a jusante corrige.

Homônimo entre categorias não é exceção: jaguar, apple, galaxy, java, mustang,
delta. Numa plataforma que pretende medir qualquer produto do mundo, a
suposição "um nome, um produto" quebra cedo e em silêncio — a análise sai
perfeita, medindo outra coisa.

A identidade passa a ser (nome normalizado, categoria). Categoria NULA continua
existindo e participa da chave como "sem categoria declarada", que é uma
resposta diferente de qualquer categoria concreta.

O ALIAS SÓ DECIDE QUANDO É INEQUÍVOCO. Com o nome apontando para duas
categorias e o pedido sem categoria, o passo 1 se recusa a responder e a
pergunta vira de SIGNIFICADO, que é o passo 3. Preferir "não sei" a chutar é o
que impede o erro barato e permanente.
"""

import sqlalchemy as sa
from alembic import op

revision = "051"
down_revision = "050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("product_aliases", sa.Column("category", sa.String(), nullable=True))
    # A categoria de quem já está no catálogo, para as linhas existentes.
    op.execute(
        "UPDATE product_aliases a SET category = p.category "
        "FROM product_catalog p WHERE p.entity_key = a.entity_key"
    )
    op.drop_index("ix_product_aliases_alias_normalized", table_name="product_aliases")
    # COALESCE porque nulo não participa de unicidade no Postgres: sem isso, o
    # mesmo nome sem categoria poderia ser gravado várias vezes.
    op.execute(
        "CREATE UNIQUE INDEX ix_product_aliases_nome_categoria "
        "ON product_aliases (alias_normalized, COALESCE(category, ''))"
    )
    op.create_index("ix_product_aliases_nome", "product_aliases", ["alias_normalized"])


def downgrade() -> None:
    op.drop_index("ix_product_aliases_nome", table_name="product_aliases")
    op.execute("DROP INDEX IF EXISTS ix_product_aliases_nome_categoria")
    # Volta a caber um nome só: o que sobra é a linha mais antiga de cada nome.
    op.execute(
        "DELETE FROM product_aliases a USING product_aliases b "
        "WHERE a.alias_normalized = b.alias_normalized AND a.first_seen_at > b.first_seen_at"
    )
    op.create_index(
        "ix_product_aliases_alias_normalized", "product_aliases", ["alias_normalized"], unique=True
    )
    op.drop_column("product_aliases", "category")
