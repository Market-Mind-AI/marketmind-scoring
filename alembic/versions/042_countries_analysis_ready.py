"""Separa "o país existe no catálogo" de "o país pode ser analisado".

2026-09-23. A tabela tinha 24 linhas e todas eram oferecidas no seletor do
front, então as duas coisas eram a mesma. Deixam de ser no momento em que se
quer acrescentar os candidatos.

POR QUE NÃO BASTA ACRESCENTAR. A regra do grafo é que nada parcial é entregue:
um país pedido que não pontua faz a análise INTEIRA desistir, levando junto os
países bons escolhidos na mesma consulta. Oferecer um país sem medir antes é
vender uma falha.

E "a API lista o país" não é "a API tem o dado dele". Medido em 23/09/2026
contra a API pública do World Bank, país por país: dos 217 do catálogo, 159
têm os 12 indicadores e 58 não. Nigéria, por exemplo, não tem 5 deles.

A primeira medição errou, e o erro vale registrar: tratava falha de rede como
ausência de dado, e 2604 requisições seguidas tomam limite de taxa. O resultado
dizia que o Japão não tem população. Remedindo com repetição, e tratando o que
não se conseguiu ler como "não sei" em vez de "não tem", os 24 países que já
funcionam passam todos — que é a verificação de sanidade que faltava.

Por isso a coluna nasce FALSA para quem chegar depois: um país entra no
catálogo como candidato e só é oferecido quando alguém mediu que ele pontua.
Os 24 atuais nascem verdadeiros porque a prova deles são as análises que já
entregaram.
"""

import sqlalchemy as sa
from alembic import op

revision = "042"
down_revision = "041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "countries",
        sa.Column("analysis_ready", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # Os que já estão na tabela hoje são exatamente os que já entregaram
    # análise. A prova é o histórico, não uma medição nova.
    op.execute("UPDATE countries SET analysis_ready = true")


def downgrade() -> None:
    op.drop_column("countries", "analysis_ready")
