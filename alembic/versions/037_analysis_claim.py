"""Quem está executando cada análise agora, e desde quando.

2026-09-21. O `status` sozinho não responde isso. Uma análise fica `running` por
horas legitimamente, e `running` não distingue "há um processo trabalhando
nisso" de "o processo que trabalhava nisso morreu com o pod".

Sem essa distinção a retomada só podia agir por silêncio: esperar 15 minutos sem
batimento antes de reinvocar. Quinze minutos com o usuário olhando para uma tela
que diz "em andamento" e nada acontecendo — e o agente SABIA que tinha acabado
de reiniciar, ele só não tinha como dizer quais análises eram órfãs.

Com a reivindicação, um agente que sobe retoma na hora tudo que ninguém segura,
e dois agentes podem rodar lado a lado sem um roubar a análise do outro.

`claimed_at` é renovado a cada progresso, então uma reivindicação velha é um
processo morto. Ela não substitui a varredura: continua sendo a rede que pega o
caso em que ninguém subiu para reclamar.
"""

import sqlalchemy as sa
from alembic import op

revision = "037"
down_revision = "036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("analysis_requests", sa.Column("claimed_by", sa.String(), nullable=True))
    op.add_column("analysis_requests", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("analysis_requests", "claimed_at")
    op.drop_column("analysis_requests", "claimed_by")
