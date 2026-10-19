import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Index, Numeric
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class IndicatorWeight(Base):
    """Pesos das dimensões, versionados por período — Rank Reciprocal sobre a
    ordem de prioridade explícita (app/services/mos/rank_reciprocal.py), nunca
    recalculados a partir de dados nem por consulta (correção ao Cap. 5 §5.8,
    ver marketmind_mos_engine). indicator_code guarda o valor da dimensão (Cap. 5
    §5.5 — agregação em 2 estágios: o peso incide sobre as dimensões,
    não sobre indicadores crus), redundante com a coluna dimension por
    conveniência de query. 100% país/período, sem exceção por produto
    (correção de 2026-07-03 — ver marketmind_mos_engine): mesmo o peso de
    Demand, que não tem indicador país-a-país próprio, é calibrado a partir
    de uma agregação entre produtos já computados por país, nunca por um
    produto específico — garante que dois produtos diferentes sejam sempre
    somados com a mesma fórmula. Imutável após period_key fechar, igual a
    normalization_reference."""

    __tablename__ = "indicator_weights"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    indicator_code: Mapped[str] = mapped_column(nullable=False)
    dimension: Mapped[str] = mapped_column(nullable=False)
    period_key: Mapped[str] = mapped_column(nullable=False)
    weight: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    is_closed: Mapped[bool] = mapped_column(nullable=False, default=False)
    computed_at: Mapped[datetime] = mapped_column(nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_indicator_weights_dimension", "dimension"),
        Index("ix_indicator_weights_lookup", "indicator_code", "period_key", unique=True),
    )
