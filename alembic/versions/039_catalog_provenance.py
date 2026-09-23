"""Guarda COMO o produto desta análise foi identificado.

2026-09-23. `entity_key` diz QUAL produto; nada dizia como se chegou nele. A
resolução tem quatro desfechos muito diferentes entre si — alias exato,
similaridade de forma (`pg_trgm`), decisão do LLM entre candidatos da mesma
partição de marca, ou criar entidade nova — e o quarto é o único que significa
"este produto nunca foi visto aqui". Confundir os quatro é confundir catálogo
que funciona com catálogo que duplica.

Fica na análise, e não só no log da requisição, por dois motivos. O trace do
Langfuse é montado quando o grafo roda, e a resolução acontece ANTES disso, no
POST /analyze — sem um lugar durável o fato não teria como entrar no trace. E
uma análise retomada não resolve o produto de novo: leria o próprio registro e
encontraria o campo vazio, relatando "não sei" para algo que já se soube.

`catalog_match_score` só tem valor quando o método é `trigram`; é a similaridade
que decidiu. Nos outros é nulo, porque não existe — não é zero.
"""

import sqlalchemy as sa
from alembic import op

revision = "039"
down_revision = "038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("analysis_requests", sa.Column("catalog_method", sa.String(), nullable=True))
    op.add_column("analysis_requests", sa.Column("catalog_match_score", sa.Numeric(), nullable=True))


def downgrade() -> None:
    op.drop_column("analysis_requests", "catalog_match_score")
    op.drop_column("analysis_requests", "catalog_method")
