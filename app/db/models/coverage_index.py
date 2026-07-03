import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CoverageIndex(Base):
    """O que o Core sabe que já tem coletado, por indicador — nunca os 4 serviços
    de coleta. Usado para decidir o diff de cobertura antes de disparar qualquer
    coleta nova (ver marketmind_core_architecture)."""

    __tablename__ = "coverage_index"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source: Mapped[str] = mapped_column(nullable=False)
    entity_key: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("product_catalog.entity_key"), nullable=True
    )
    country: Mapped[str] = mapped_column(String(3), nullable=False)
    indicator_code: Mapped[str] = mapped_column(nullable=False)
    period_key: Mapped[str] = mapped_column(nullable=False)
    is_closed: Mapped[bool] = mapped_column(nullable=False, default=False)
    last_fetched_at: Mapped[datetime] = mapped_column(nullable=False, default=datetime.utcnow)
    ttl_days: Mapped[int | None] = mapped_column(nullable=True)

    __table_args__ = (
        Index(
            "ix_coverage_index_lookup_with_entity",
            "source",
            "entity_key",
            "country",
            "indicator_code",
            "period_key",
            unique=True,
            postgresql_where=text("entity_key IS NOT NULL"),
        ),
        Index(
            "ix_coverage_index_lookup_country_level",
            "source",
            "country",
            "indicator_code",
            "period_key",
            unique=True,
            postgresql_where=text("entity_key IS NULL"),
        ),
        Index("ix_coverage_index_is_closed", "is_closed"),
    )
