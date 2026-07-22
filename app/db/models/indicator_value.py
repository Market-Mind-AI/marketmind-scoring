import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Index, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class IndicatorValue(Base):
    """Camada cacheável de indicadores do Cap. 4, já normalizados. Segue o
    contrato funcional da seção 4.8 (indicator_code, source, raw_value,
    normalized_score, direction, calculation_window, availability_status,
    notes). period_type/period_key permitem reprocessar a fórmula bruta sobre
    os registros agregados de vários meses, sem depender de médias de scores
    já prontos.

    business_context/interpretation_band (2026-07-08, ver
    marketmind_mos_engine): camada de contexto pro LLM, determinística (não
    gerada por IA) — a frase fixa de "o que esse indicador significa"
    (app/services/mos/interpretation.py::INDICATOR_MEANING) e a faixa
    categórica do normalized_score (Very Low/Low/Moderate/High/Very High).
    Diferente de `notes`, que é trilha de auditoria (por que caiu em
    fallback/unavailable), não significado de negócio."""

    __tablename__ = "indicator_values"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entity_key: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("product_catalog.entity_key"), nullable=True
    )
    country: Mapped[str] = mapped_column(String(3), nullable=False)
    indicator_code: Mapped[str] = mapped_column(nullable=False)
    period_type: Mapped[str] = mapped_column(nullable=False)
    period_key: Mapped[str] = mapped_column(nullable=False)
    source: Mapped[str] = mapped_column(nullable=False)
    raw_value: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    normalized_score: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    direction: Mapped[str | None] = mapped_column(nullable=True)
    calculation_window: Mapped[str | None] = mapped_column(nullable=True)
    availability_status: Mapped[str] = mapped_column(nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    business_context: Mapped[str | None] = mapped_column(Text, nullable=True)
    interpretation_band: Mapped[str | None] = mapped_column(nullable=True)
    # Frase determinística que explica ESTE número: label + banda + score + a
    # frase da faixa (metric_band_phrases) + o detalhe concreto quando houver
    # (ex.: volume/população/penetração da Demand Volume). Montada no cálculo e
    # levada ao LLM final. Ver app/services/mos/explanation_builder.py.
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index(
            "ix_indicator_values_lookup_with_entity",
            "entity_key",
            "country",
            "indicator_code",
            "period_type",
            "period_key",
            unique=True,
            postgresql_where=text("entity_key IS NOT NULL"),
        ),
        Index(
            "ix_indicator_values_lookup_country_level",
            "country",
            "indicator_code",
            "period_type",
            "period_key",
            unique=True,
            postgresql_where=text("entity_key IS NULL"),
        ),
        Index("ix_indicator_values_country", "country"),
        Index("ix_indicator_values_indicator_code", "indicator_code"),
    )
