import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class ProductAlias(Base):
    """Pipeline de resolução de identidade, passo 1 (o mais barato): alias exato
    normalizado -> entity_key. alias_normalized é único: um texto normalizado
    sempre resolve para a mesma entidade."""

    __tablename__ = "product_aliases"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    alias_normalized: Mapped[str] = mapped_column(nullable=False)
    entity_key: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("product_catalog.entity_key"), nullable=False
    )
    source: Mapped[str] = mapped_column(nullable=False)
    hit_count: Mapped[int] = mapped_column(nullable=False, default=1)
    first_seen_at: Mapped[datetime] = mapped_column(nullable=False, default=datetime.utcnow)

    product: Mapped["ProductCatalog"] = relationship(back_populates="aliases", foreign_keys=[entity_key])  # type: ignore[name-defined]  # noqa: F821

    __table_args__ = (
        Index("ix_product_aliases_alias_normalized", "alias_normalized", unique=True),
        Index("ix_product_aliases_entity_key", "entity_key"),
        # Pipeline passo 2 (fuzzy trigram) — catalog_service.CatalogService._trigram_search.
        Index(
            "ix_product_aliases_alias_normalized_trgm",
            "alias_normalized",
            postgresql_using="gin",
            postgresql_ops={"alias_normalized": "gin_trgm_ops"},
        ),
    )
