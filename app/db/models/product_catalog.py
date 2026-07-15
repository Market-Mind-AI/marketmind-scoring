import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, Index
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import settings
from app.db.base import Base


class ProductCatalog(Base):
    """Identidade de produto determinística. entity_key é sempre gerado localmente
    (uuid4), nunca bloqueado por dependência externa. google_mid é enriquecimento
    best-effort/assíncrono, nunca requisito para a entidade existir. embedding
    alimenta o passo 3 (semântico) do pipeline de resolução em catalog_service —
    nulo até a entidade passar por esse passo (nunca bloqueia a criação)."""

    __tablename__ = "product_catalog"

    entity_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    canonical_title: Mapped[str] = mapped_column(nullable=False)
    google_mid: Mapped[str | None] = mapped_column(nullable=True)
    google_type: Mapped[str | None] = mapped_column(nullable=True)
    category: Mapped[str | None] = mapped_column(nullable=True)
    # Per-COUNTRY curated search terms (2026-07-15), keyed by ISO3:
    #   {"<ISO3>": {"primary_name", "formal_name"?, "search_terms": [...],
    #               "excluded_terms": [...], "confidence": float}}
    # Produced by the identify-image LLM (image + target countries) and
    # user-reviewable. The demand LEVEL = SUM of the absolute Google Ads search
    # volumes of a country's `search_terms` (deduped); `excluded_terms` filter
    # ambiguous collisions; `search_terms` also seed the related-keyword /
    # intent call. Replaces the old Trends-era {lang: [names]} `localized_terms`.
    # See marketmind_mos_engine.
    localized_markets: Mapped[dict[str, dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    target_age_min: Mapped[int | None] = mapped_column(nullable=True)
    target_age_max: Mapped[int | None] = mapped_column(nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(settings.openai_embedding_dimensions), nullable=True)
    merged_into: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("product_catalog.entity_key"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=datetime.utcnow)

    aliases: Mapped[list["ProductAlias"]] = relationship(  # type: ignore[name-defined]  # noqa: F821
        back_populates="product", foreign_keys="ProductAlias.entity_key"
    )

    __table_args__ = (
        Index("ix_product_catalog_google_mid", "google_mid"),
        Index("ix_product_catalog_merged_into", "merged_into"),
        Index("ix_product_catalog_category", "category"),
    )
