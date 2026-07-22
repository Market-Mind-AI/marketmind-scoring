import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Index, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MosResult(Base):
    """MOS por país, sempre independente — nunca um valor agregado combinando
    países de uma mesma consulta (Cap. 5, ver marketmind_mos_engine).
    mos_before_cap/capped_by expõem quando o teto anticompensação (baseado no
    Trend Score) reduziu o MOS final, para transparência na explicação do LLM.
    library_topsis_score/library_mos guardam o mesmo cálculo usando o TOPSIS +
    Entropy Weight *clássicos* (lib pymcdm — ideal/anti-ideal relativos à
    população comparada, com a normalização vetorial padrão), só pra
    comparação/validação manual contra a nossa implementação corrigida — não
    são usados em nenhuma decisão do produto, ver
    app/services/mos/library_reference.py.

    mos_interpretation (2026-07-08, ver marketmind_mos_engine): rótulo fixo
    do Cap. 5 §5.8 (ex.: "Good Opportunity"), calculado por
    app/services/mos/interpretation.py::mos_band — contexto pro LLM, não
    afeta o valor de `mos`."""

    __tablename__ = "mos_results"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entity_key: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("product_catalog.entity_key"), nullable=False
    )
    country: Mapped[str] = mapped_column(String(3), nullable=False)
    period_type: Mapped[str] = mapped_column(nullable=False)
    period_key: Mapped[str] = mapped_column(nullable=False)
    mos: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    mos_before_cap: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    capped_by: Mapped[str | None] = mapped_column(nullable=True)
    topsis_score: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    library_topsis_score: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    library_mos: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    mos_interpretation: Mapped[str | None] = mapped_column(nullable=True)
    # Frase determinística que explica ESTE MOS (label + banda + score + a frase
    # da faixa do MOS de metric_band_phrases), levada ao LLM final junto do
    # breakdown das dimensões. Ver app/services/mos/explanation_builder.py.
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    availability_status: Mapped[str] = mapped_column(nullable=False)
    excluded_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_mos_results_lookup", "entity_key", "country", "period_type", "period_key", unique=True),
        Index("ix_mos_results_country", "country"),
    )
