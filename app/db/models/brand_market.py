"""O vocabulário de busca de uma MARCA dentro de um produto, por país.

A unidade do catálogo é o produto — `coffee`, não `Juan Valdez coffee`. Mas o
que se mede no Google Ads é outro para cada marca: quem procura "juan valdez
café" não procura "café", e as duas buscas nunca podem dar o mesmo número.

Então o vocabulário tem dono explícito:

    sem marca   `product_catalog.localized_markets` — o vocabulário do produto
    com marca   esta tabela, chaveada por (produto, marca, país)

Guardar os termos da marca no produto foi o estrago que a partição de marca
existia para evitar: medido em 27/08/2026, `single malt Scotch whisky` +
Cardhu e + Glenfiddich caíam na mesma entidade e a segunda marca herdava o
vocabulário e a demanda da primeira. Aqui isso não acontece porque cada marca
tem a própria linha.

O JSON é o MESMO formato de `localized_markets` de propósito, para que quem lê
não precise saber de onde veio: `{primary_name, formal_name, search_terms,
excluded_terms, search_language, confidence}`.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.utils.time import utc_now


class BrandMarket(Base):
    __tablename__ = "brand_markets"

    entity_key: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("product_catalog.entity_key"), primary_key=True
    )
    brand_key: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("brands.brand_key"), primary_key=True
    )
    country: Mapped[str] = mapped_column(String(3), primary_key=True)
    market: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(nullable=False, default=utc_now, onupdate=utc_now)
