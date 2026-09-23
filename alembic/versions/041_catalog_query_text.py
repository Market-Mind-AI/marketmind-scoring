"""Guarda O QUE foi perguntado, ao lado do que o catálogo respondeu.

2026-09-23. As migrations 039 e 040 guardaram como a entidade foi encontrada.
Faltava o outro lado do par: o texto que o comprador digitou.

Sem ele o registro confunde. Medido no mesmo dia: uma análise pedida como "top
sirloin cap" aparecia no trace como "'picanha': coincidência exata, um alias já
apontava para ela" — verdadeiro e ilegível, porque a palavra `picanha` não
estava em lugar nenhum do pedido. Quem lê precisa do par: o que foi perguntado e
em que isso foi parar.

É também o que torna a coincidência AUDITÁVEL. Um match de 41% entre "cafetera
italiana para espresso" e "cafeteira italiana moka" só pode ser julgado por
alguém que veja os dois lados; com um lado só, a porcentagem não significa nada.
"""

import sqlalchemy as sa
from alembic import op

revision = "041"
down_revision = "040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("analysis_requests", sa.Column("catalog_query_text", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("analysis_requests", "catalog_query_text")
