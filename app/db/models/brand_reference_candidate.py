"""Os mercados de referência de uma MARCA dentro de um produto.

O maior volume real entre eles é o TETO da régua de Demand Volume (Cap. 4
§4.2.1). Onde o café é mais procurado não é onde a Juan Valdez é mais
procurada, então usar a lista da categoria para medir a marca desloca todos os
scores de demanda dela de uma vez.

Sem marca, a lista continua em `product_catalog.reference_candidates`.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.utils.time import utc_now


class BrandReferenceCandidate(Base):
    __tablename__ = "brand_reference_candidates"

    entity_key: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("product_catalog.entity_key"), primary_key=True
    )
    brand_key: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("brands.brand_key"), primary_key=True
    )
    candidates: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False, default=utc_now, onupdate=utc_now)
