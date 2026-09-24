import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index
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
    # A identidade é (nome, categoria), não o nome sozinho: `jaguar` em
    # Automotive e `jaguar` em Wildlife são produtos diferentes. Nulo é "sem
    # categoria declarada", que é uma resposta e não um curinga.
    category: Mapped[str | None] = mapped_column(nullable=True)
    entity_key: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("product_catalog.entity_key"), nullable=False
    )
    source: Mapped[str] = mapped_column(nullable=False)
    hit_count: Mapped[int] = mapped_column(nullable=False, default=1)
    first_seen_at: Mapped[datetime] = mapped_column(nullable=False, default=datetime.utcnow)

    product: Mapped["ProductCatalog"] = relationship(back_populates="aliases", foreign_keys=[entity_key])  # type: ignore[name-defined]  # noqa: F821

    __table_args__ = (
        # Alias vazio é um ímã: como `alias_normalized` é único e o passo 1
        # casa por igualdade exata, uma única linha vazia faria toda consulta
        # sem forma normalizada resolver para a mesma entidade. Ver migration
        # 028 e app/utils/text.normalize_alias.
        CheckConstraint("btrim(alias_normalized) <> ''", name="ck_product_aliases_alias_normalized_nao_vazio"),
        # A unicidade real vive no índice da migration 051, que usa
        # COALESCE(category, '') — nulo não participa de unicidade no Postgres,
        # e sem o COALESCE o mesmo nome sem categoria entraria várias vezes.
        # Aqui fica só a busca.
        Index("ix_product_aliases_nome", "alias_normalized"),
        Index("ix_product_aliases_entity_key", "entity_key"),
        # Pipeline passo 2 (fuzzy trigram) — catalog_service.CatalogService._trigram_search.
        Index(
            "ix_product_aliases_alias_normalized_trgm",
            "alias_normalized",
            postgresql_using="gin",
            postgresql_ops={"alias_normalized": "gin_trgm_ops"},
        ),
    )
