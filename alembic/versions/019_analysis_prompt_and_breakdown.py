"""analysis_requests.user_prompt + breakdown

2026-07-24. Two additive columns on analysis_requests:

- `user_prompt` (Text): the buyer's own free-text description/question typed on
  the frontend. Previously only used as the identify-image vision hint and then
  lost. Now stored so the final explanation LLM can ANSWER what the user asked —
  strictly from the deterministic evidence packet, never inventing data (Cap. 4
  §4.0). Nullable: analyses submitted without a prompt still run.

- `breakdown` (JSONB): the full per-country evidence packet the explanation
  service already assembles (glossary + Rank-Reciprocal weights + demand ruler +
  each country's MOS, the 7 dimension subscores, and every deterministic signal
  with its stored explanation sentence). It was computed then discarded once the
  narrative string was produced; persisting it lets the frontend render the rich
  MOS modal (all scores + metrics + explanations) from a single GET, and keeps a
  durable record of exactly what fed each MOS. Nullable: older rows and
  best-effort gaps rebuild lazily from indicator_values/mos_results on read.

Revision ID: 019
Revises: 018
Create Date: 2026-07-24

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "019"
down_revision: str | None = "018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("analysis_requests", sa.Column("user_prompt", sa.Text(), nullable=True))
    op.add_column("analysis_requests", sa.Column("breakdown", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("analysis_requests", "breakdown")
    op.drop_column("analysis_requests", "user_prompt")
