import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Index
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Job(Base):
    """Orquestração assíncrona de coleta (nunca de cálculo de MOS, que é sempre
    síncrono). Representa um pedido do usuário que precisou de coleta de dados
    faltantes antes de poder responder."""

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(nullable=False)
    current_step: Mapped[str | None] = mapped_column(nullable=True)
    requested_by: Mapped[str | None] = mapped_column(nullable=True)
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)

    tasks: Mapped[list["JobTask"]] = relationship(back_populates="job")  # type: ignore[name-defined]  # noqa: F821

    __table_args__ = (Index("ix_jobs_status", "status"),)
