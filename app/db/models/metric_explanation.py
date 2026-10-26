import uuid

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MetricExplanation(Base):
    """Camada de explicação PARAMETRIZADA no banco (2026-07-22, pedido do
    usuário: "todos os scores devem ter sua explicação... tudo parametrizado no
    banco"). Uma linha por métrica — cada indicador do Cap. 4, cada dimensão do
    Cap. 5 e o próprio MOS — com o rótulo legível, a frase de significado
    (o que a métrica mede / como ler) e a direção (higher_is_better).

    É a FONTE EDITÁVEL em runtime: mudar a frase aqui muda a explicação em todo
    lugar, sem deploy. Semeada com os defaults de
    app/services/mos/explanation_seed.py (que importa os significados
    canônicos de interpretation.py). As frases por FAIXA ficam em
    metric_band_phrases. Determinístico — o LLM final só narra, nunca recalcula
    (Cap. 4 §4.0)."""

    __tablename__ = "metric_explanations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    metric_code: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    metric_kind: Mapped[str] = mapped_column(String, nullable=False)  # indicator | dimension | mos
    label: Mapped[str] = mapped_column(String, nullable=False)
    meaning: Mapped[str] = mapped_column(Text, nullable=False)
    # None = direção-neutra (context only, sem valência maior/menor).
    higher_is_better: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # {idioma: {"label": ..., "meaning": ...}} para pt e es (migration 062). O
    # inglês das colunas acima é o canônico e o último recurso.
    translations: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (Index("ix_metric_explanations_code", "metric_code", unique=True),)


class MetricBandPhrase(Base):
    """Frase por FAIXA de cada métrica (Very Low..Very High para
    indicadores/dimensões; Very Low Opportunity..Excellent Opportunity para o
    MOS). É o que dá a leitura em palavras do NÚMERO de cada score, montada em
    runtime junto do valor/banda e gravada na coluna `explanation` do próprio
    score (indicator_values / mos_results). Editável no banco. Ver
    metric_explanations e app/services/mos/explanation_seed.py."""

    __tablename__ = "metric_band_phrases"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    metric_code: Mapped[str] = mapped_column(
        String, ForeignKey("metric_explanations.metric_code", ondelete="CASCADE"), nullable=False
    )
    band: Mapped[str] = mapped_column(String, nullable=False)
    phrase: Mapped[str] = mapped_column(Text, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)  # ordem da faixa (0 = pior/menor)
    # {idioma: frase} para pt e es (migration 062).
    translations: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (Index("ix_metric_band_phrases_code_band", "metric_code", "band", unique=True),)
