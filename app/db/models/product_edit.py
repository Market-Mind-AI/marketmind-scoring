"""Cada edição que um usuário fez num produto do catálogo, conferida.

O catálogo é compartilhado: a faixa etária que uma pessoa muda na tela de
revisão muda a nota de quem analisar o produto depois. Por isso nenhuma edição
entra em silêncio — o modelo confere se ela faz sentido para ESTE produto
(CatalogService.revisar_edicoes), e esta tabela guarda o que foi pedido, o que
havia antes, a decisão e o porquê. É o que permite explicar uma nota que mudou
e desfazer uma edição aceita.
"""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.utils.time import utc_now


class ProductEdit(Base):
    __tablename__ = "product_edits"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entity_key: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("product_catalog.entity_key"), nullable=False, index=True
    )
    field: Mapped[str] = mapped_column(nullable=False)
    old_value: Mapped[str | None] = mapped_column(nullable=True)
    new_value: Mapped[str | None] = mapped_column(nullable=True)
    accepted: Mapped[bool] = mapped_column(nullable=False)
    reason: Mapped[str | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utc_now)
