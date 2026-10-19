"""O idioma em que a narrativa desta análise deve sair.

2026-10-19. A narrativa saía no idioma configurado no serviço
(`OLLAMA_LLM_NARRATIVE_LANGUAGE`, PT por padrão) enquanto a interface é EN/ES:
quem lia o relatório em espanhol recebia a explicação em português. O idioma é
uma escolha de quem pediu, não do serviço, então viaja com o pedido — o
frontend manda o idioma da interface, o orchestrator o registra aqui e o lê de
volta na hora de narrar (e de revisar).

NULO é "o configurado": toda análise anterior a esta migration continua
valendo como antes, e um cliente que não manda idioma recebe o padrão.
Valores: `pt`, `en`, `es` — os três que têm instrução própria em
app/services/explanation_client.py; a validação é do schema, não do banco.

Revision ID: 055
Revises: 054
"""

import sqlalchemy as sa

from alembic import op

revision = "055"
down_revision = "054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("analysis_requests", sa.Column("language", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("analysis_requests", "language")
