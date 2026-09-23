"""Guarda POR QUE o passo semântico disse que é o mesmo produto.

2026-09-23. A migration 039 guardou o método e o score. Faltava o caso em que
não existe score: o passo `llm`, que é justamente o único que decide por
SIGNIFICADO. `quadcopter` e `drone` não têm semelhança de escrita nenhuma —
nenhum número descreve o que aconteceu ali, e a frase do modelo descreve.

Vale lembrar por que o passo é um LLM e não um vetor. A coluna
`product_catalog.embedding` (vector(1536)) existiu até 14/08/2026 e foi medida
contra os 89 termos reais do catálogo: com os `search_terms` indexados como
alias, o trigram resolve 71 deles no limiar 0.4 com precisão de 100%. Os 18 que
sobram não têm parentesco de escrita com nada indexado (`odżywka bez
spłukiwania`, `lockencreme`) ou são sinônimo de domínio. Nenhuma distância de
cosseno separa esses casos, porque `quadcopter`~`drone` é conhecimento e não
proximidade de string. Ver a migration 027 e config.py.

Nulo em `alias`, `trigram` e `created`: nos dois primeiros a coincidência é de
escrita e tem número próprio, e no último não houve coincidência alguma.
"""

import sqlalchemy as sa
from alembic import op

revision = "040"
down_revision = "039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("analysis_requests", sa.Column("catalog_match_reasoning", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("analysis_requests", "catalog_match_reasoning")
