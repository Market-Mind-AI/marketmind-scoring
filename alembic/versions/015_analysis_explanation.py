"""analysis_requests.explanation — final LLM narrative of the MOS ranking

2026-07-16. The MOS result is deterministic (Cap. 4/5); this column stores the
business-facing NARRATIVE generated from it by the final LLM inference service
(marketmind-ollma-llm, a local Ollama qwen3:14b). Best-effort: null when that
service is unavailable — the analysis still completes with the numbers. The LLM
only explains, it never computes or alters any score (Cap. 4 §4.0).

Revision ID: 015
Revises: 014
Create Date: 2026-07-16

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "015"
down_revision: str | None = "014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("analysis_requests", sa.Column("explanation", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("analysis_requests", "explanation")
