"""Analysis requests (Fase 5 — POST /analyze)

New table for the async orchestration endpoint: one row per frontend
request (product + countries), separate from jobs/job_tasks (which model
one collection task each, a smaller granularity). Processed via FastAPI
BackgroundTasks (v1 — no dedicated worker/queue yet, see memory
marketmind_mos_engine).

Revision ID: 009
Revises: 008
Create Date: 2026-07-08

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "009"
down_revision: str | None = "008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "analysis_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column(
            "entity_key", postgresql.UUID(as_uuid=True), sa.ForeignKey("product_catalog.entity_key"), nullable=True
        ),
        sa.Column("canonical_title", sa.String(), nullable=True),
        sa.Column("countries", postgresql.JSONB(), nullable=False),
        sa.Column("estimated_price", sa.Numeric(), nullable=True),
        sa.Column("price_currency", sa.String(), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_analysis_requests_status", "analysis_requests", ["status"])


def downgrade() -> None:
    op.drop_index("ix_analysis_requests_status", table_name="analysis_requests")
    op.drop_table("analysis_requests")
