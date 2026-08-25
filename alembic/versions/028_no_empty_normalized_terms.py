"""Proíbe alias e termo excluído com forma normalizada vazia.

Até 2026-08-25, `normalize_alias` apagava tudo fora de [a-z0-9], então um texto
inteiramente em japonês, chinês, coreano, russo ou árabe normalizava para
string VAZIA — e `resolve()` gravava esse vazio como alias. Reproduzido no
banco de teste: 'ノンフライヤー' (air fryer), '空气炸锅' (air fryer) e
'Пылесос-робот' (aspirador robô) resolviam para a MESMA entidade, porque o
segundo e o terceiro casavam com o alias vazio do primeiro pelo passo 1. Por
ser o caminho O(1), o veto por evidência negativa nem chegava a rodar.

A causa foi corrigida no normalizador (que agora preserva letra e dígito de
qualquer escrita), e o serviço passou a recusar gravar alias sem forma
normalizada. Esta CHECK é a terceira linha: torna o estado corrompido
impossível de existir, em vez de apenas improvável. Um texto só de pontuação ou
emoji continua normalizando para vazio, e é justamente esse caso que a
aplicação agora deixa passar sem alias.

Revision ID: 028
Revises: 027
"""

from alembic import op

revision: str = "028"
down_revision: str | None = "027"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Limpeza antes da restrição. Alias vazio é sempre lixo: nunca foi
    # gravado de propósito e não identifica produto nenhum.
    op.execute("DELETE FROM product_aliases WHERE btrim(alias_normalized) = ''")
    op.execute("DELETE FROM product_excluded_terms WHERE btrim(term_normalized) = ''")
    op.execute(
        "ALTER TABLE product_aliases ADD CONSTRAINT ck_product_aliases_alias_normalized_nao_vazio "
        "CHECK (btrim(alias_normalized) <> '')"
    )
    op.execute(
        "ALTER TABLE product_excluded_terms ADD CONSTRAINT ck_product_excluded_terms_term_nao_vazio "
        "CHECK (btrim(term_normalized) <> '')"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE product_excluded_terms DROP CONSTRAINT ck_product_excluded_terms_term_nao_vazio")
    op.execute("ALTER TABLE product_aliases DROP CONSTRAINT ck_product_aliases_alias_normalized_nao_vazio")
