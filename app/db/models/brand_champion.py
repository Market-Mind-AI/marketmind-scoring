"""O melhor mercado de referência de uma MARCA, num período.

Sem marca, o campeão fica em `product_catalog.champion_*`, onde sempre esteve.
Com marca, aqui — porque o produto passou a ser um só e compartilhar o campeão
entre marcas repetiria a contaminação que `brand_markets` existe para impedir.

E essa em especial se autoconfirma: campeão conhecido faz a análise PULAR a
coleta dos mercados de referência, que é o passo mais caro do sistema. Um
campeão herdado de outra marca nunca seria corrigido, porque existir já impede
a medição que o desmentiria.

`country` nulo com `period_key` preenchido é "nada pontuável neste período" —
uma resposta, não uma ausência, e é ela que impede a análise seguinte de
refazer o mesmo trabalho infrutífero.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.utils.time import utc_now


class BrandChampion(Base):
    __tablename__ = "brand_champions"

    entity_key: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("product_catalog.entity_key"), primary_key=True
    )
    brand_key: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("brands.brand_key"), primary_key=True
    )
    period_key: Mapped[str] = mapped_column(primary_key=True)
    country: Mapped[str | None] = mapped_column(String(3), nullable=True)
    mos: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(nullable=False, default=utc_now)
    # A régua com que foi calculado; o campeão só vale para ela (migration 061).
    demand_ceiling: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
