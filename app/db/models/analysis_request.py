import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import ForeignKey, Index, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AnalysisRequest(Base):
    """Fase 5 — POST /analyze. Representa um pedido do frontend de ponta a
    ponta (produto + países), diferente de `Job`/`JobTask` (que modelam uma
    *task* de coleta individual, granularidade menor). Processado via
    `BackgroundTasks` do FastAPI (v1 — sem worker/fila dedicada ainda, ver
    memory marketmind_mos_engine): se a API reiniciar no meio do
    processamento, o registro fica travado em `running` sem retomar —
    limitação aceita explicitamente pelo usuário.

    `estimated_price`/`price_currency` vêm de `POST /catalog/identify-image`
    (uma estimativa da LLM a partir da foto, não de API real) — guardados só
    como contexto, nunca entram em nenhum cálculo de indicador ou MOS.
    """

    __tablename__ = "analysis_requests"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(nullable=False)
    entity_key: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("product_catalog.entity_key"), nullable=True
    )
    canonical_title: Mapped[str | None] = mapped_column(nullable=True)
    countries: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    estimated_price: Mapped[Decimal | None] = mapped_column(nullable=True)
    price_currency: Mapped[str | None] = mapped_column(nullable=True)
    result: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (Index("ix_analysis_requests_status", "status"),)
