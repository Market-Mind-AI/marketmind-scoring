"""Batimento da análise, para saber quando ela morreu em vez de estar demorando.

2026-08-27. Com a decisão D3 uma análise legítima pode ficar horas em
`running`, insistindo numa fonte fora do ar. Isso torna impossível distinguir,
só pelo status, o pedido que está trabalhando do que morreu junto com o pod —
e sem essa distinção "nada parcial sai" vira "às vezes não sai nada".

`heartbeat_at` é atualizado a cada país coberto e a cada tentativa do laço de
insistência. Quem varre para retomar (analysis_resume_service) só toca no que
está calado há mais tempo que o passo mais longo, então uma análise viva nunca
é retomada em paralelo com ela mesma — o que importaria se um dia rodar com
mais de uma réplica.

Nulo nas linhas antigas: análise terminada não bate.

Revision ID: 034
Revises: 033
Create Date: 2026-08-27

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "034"
down_revision: str | None = "033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("analysis_requests", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("analysis_requests", "heartbeat_at")
