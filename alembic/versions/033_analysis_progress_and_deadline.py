"""A análise passa a dizer no que está esperando, e até quando.

2026-08-27, decisão D3: ranking parcial não sai. Um país sem indicador por
falha TRANSITÓRIA da fonte mantém a análise rodando em vez de ser excluído do
resultado, o que levanta duas perguntas que o registro não sabia responder:

  progress     o que ela está esperando AGORA. Sem isto, uma análise legítima
               insistindo numa fonte fora do ar é indistinguível de uma travada,
               tanto para o usuário quanto para quem for retomá-la.
  deadline_at  até quando vale insistir. Persistido, e não só em memória,
               porque quem retoma depois de um restart precisa saber se ainda
               está dentro do orçamento ou se o pedido já deveria ter falhado.

Os dois são nulos nas linhas antigas, que é o estado correto para uma análise
já terminada.

Revision ID: 033
Revises: 032
Create Date: 2026-08-27

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "033"
down_revision: str | None = "032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("analysis_requests", sa.Column("progress", sa.Text(), nullable=True))
    op.add_column("analysis_requests", sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("analysis_requests", "deadline_at")
    op.drop_column("analysis_requests", "progress")
