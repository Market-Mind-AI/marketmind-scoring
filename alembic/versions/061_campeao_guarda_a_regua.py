"""O campeão guarda a régua de demanda com que foi calculado.

2026-10-26. O campeão do período era reaproveitado só pelo período, e a régua
pode subir no meio dele: um país pedido acima de todos os candidatos entra nos
candidatos no topo. O orquestrador lia o campeão guardado, a régua subia logo
depois e o campeão era apagado, mas o que já tinha sido lido era coroado assim
mesmo — o MOS dele tinha sido calculado contra outra régua. Com a régua ao
lado, o campeão só vale para a régua de agora, e ninguém precisa lembrar de
apagá-lo.

NULO é "calculado antes desta migration": não vale para régua nenhuma e é
recalculado na primeira análise.

Revision ID: 061
Revises: 060
"""

import sqlalchemy as sa

from alembic import op

revision = "061"
down_revision = "060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("product_catalog", sa.Column("champion_demand_ceiling", sa.Numeric(), nullable=True))
    op.add_column("brand_champions", sa.Column("demand_ceiling", sa.Numeric(), nullable=True))


def downgrade() -> None:
    op.drop_column("brand_champions", "demand_ceiling")
    op.drop_column("product_catalog", "champion_demand_ceiling")
