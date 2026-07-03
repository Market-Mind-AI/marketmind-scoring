"""Job task coverage fields

Adds indicator_code and ingestion_run_id to job_tasks so a task maps exactly
to one coverage_index gap (one external collection call), and adds the
partial unique indexes used for race dedup (see app/repositories/job_repository.py).

Revision ID: 002
Revises: 001
Create Date: 2026-07-02

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "002"
down_revision: str | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("job_tasks", sa.Column("indicator_code", sa.String(), nullable=False, server_default="unknown"))
    op.alter_column("job_tasks", "indicator_code", server_default=None)
    op.add_column("job_tasks", sa.Column("ingestion_run_id", postgresql.UUID(as_uuid=True), nullable=True))

    op.create_index(
        "ix_job_tasks_in_flight_dedup_with_entity",
        "job_tasks",
        ["source", "entity_key", "country", "indicator_code", "period_key"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running') AND entity_key IS NOT NULL"),
    )
    op.create_index(
        "ix_job_tasks_in_flight_dedup_country_level",
        "job_tasks",
        ["source", "country", "indicator_code", "period_key"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running') AND entity_key IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_job_tasks_in_flight_dedup_country_level", table_name="job_tasks")
    op.drop_index("ix_job_tasks_in_flight_dedup_with_entity", table_name="job_tasks")
    op.drop_column("job_tasks", "ingestion_run_id")
    op.drop_column("job_tasks", "indicator_code")
