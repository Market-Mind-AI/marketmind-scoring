import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Index, Numeric
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class NormalizationReference(Base):
    """Bounds fixos (min/max) de normalização Min-Max, calculados contra a
    população de referência do catálogo inteiro — não contra os países de uma
    consulta específica (correção ao Cap. 4 §4.1, ver marketmind_mos_engine).
    Uma vez que period_key fecha (is_closed=true), fica imutável para sempre:
    recalcular em um mês posterior nunca sobrescreve um período já fechado."""

    __tablename__ = "normalization_reference"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    indicator_code: Mapped[str] = mapped_column(nullable=False)
    period_key: Mapped[str] = mapped_column(nullable=False)
    min_x: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    max_x: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    reference_population_size: Mapped[int] = mapped_column(nullable=False)
    is_closed: Mapped[bool] = mapped_column(nullable=False, default=False)
    computed_at: Mapped[datetime] = mapped_column(nullable=False, default=datetime.utcnow)

    __table_args__ = (Index("ix_normalization_reference_lookup", "indicator_code", "period_key", unique=True),)
