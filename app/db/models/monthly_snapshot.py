import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MonthlySnapshot(Base):
    """Índice de cadência mensal: um mês fechado (is_closed=true) nunca é
    recoletado. entity_key é nulo para indicadores país-a-país product-agnostic
    (World Bank/UN Data) que não dependem do produto consultado.

    Dois índices únicos parciais (em vez de um único composto) porque NULL nunca
    colide consigo mesmo em constraints do Postgres — sem isso, vários registros
    country-level (entity_key nulo) para o mesmo source/country/period_key não
    seriam bloqueados como duplicata."""

    __tablename__ = "monthly_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source: Mapped[str] = mapped_column(nullable=False)
    entity_key: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("product_catalog.entity_key"), nullable=True
    )
    country: Mapped[str] = mapped_column(String(3), nullable=False)
    period_key: Mapped[str] = mapped_column(nullable=False)
    is_closed: Mapped[bool] = mapped_column(nullable=False, default=False)
    collected_at: Mapped[datetime] = mapped_column(nullable=False, default=datetime.utcnow)
    raw_ref: Mapped[str | None] = mapped_column(nullable=True)

    __table_args__ = (
        Index(
            "ix_monthly_snapshots_lookup_with_entity",
            "source",
            "entity_key",
            "country",
            "period_key",
            unique=True,
            postgresql_where=text("entity_key IS NOT NULL"),
        ),
        Index(
            "ix_monthly_snapshots_lookup_country_level",
            "source",
            "country",
            "period_key",
            unique=True,
            postgresql_where=text("entity_key IS NULL"),
        ),
        Index("ix_monthly_snapshots_is_closed", "is_closed"),
    )
