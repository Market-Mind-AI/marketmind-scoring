import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class JobTask(Base):
    """Uma sub-tarefa de coleta dentro de um job — exatamente um gap de
    coverage_index (source/entity/country/indicator_code/period), ou seja,
    exatamente uma chamada de coleta a um dos 4 serviços. O job só fecha
    quando todas as tasks fecham. status=skipped significa que a cobertura já
    existia (ou já estava sendo coletada por outra task) no momento em que o
    job rodou (dedup, ver job_repository.claim_or_create_task)."""

    __tablename__ = "job_tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("jobs.id"), nullable=False)
    source: Mapped[str] = mapped_column(nullable=False)
    entity_key: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("product_catalog.entity_key"), nullable=True
    )
    country: Mapped[str] = mapped_column(String(3), nullable=False)
    indicator_code: Mapped[str] = mapped_column(nullable=False)
    period_key: Mapped[str] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(nullable=False)
    ingestion_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)

    job: Mapped["Job"] = relationship(back_populates="tasks")  # type: ignore[name-defined]  # noqa: F821

    __table_args__ = (
        Index("ix_job_tasks_job_id", "job_id"),
        Index("ix_job_tasks_status", "status"),
        # Dedup de corrida (ver job_repository): no máximo uma task
        # pending/running por gap lógico a qualquer momento. Dois índices
        # parciais em vez de um composto porque NULL nunca colide consigo
        # mesmo em constraints do Postgres (mesmo motivo de coverage_index).
        Index(
            "ix_job_tasks_in_flight_dedup_with_entity",
            "source",
            "entity_key",
            "country",
            "indicator_code",
            "period_key",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running') AND entity_key IS NOT NULL"),
        ),
        Index(
            "ix_job_tasks_in_flight_dedup_country_level",
            "source",
            "country",
            "indicator_code",
            "period_key",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running') AND entity_key IS NULL"),
        ),
    )
