"""product_catalog.reference_market -> reference_candidates (ranked shortlist)

2026-07-22 (user request). The reference market must not hinge on a single LLM
guess (it mispicked USA for air fryer, whose biggest market is actually Brazil).
Instead the enrichment LLM proposes a RANKED shortlist of up to 5 candidate
biggest-markets (with their terms); the analysis collects each one's real search
volume (reusing cached data) and the LARGEST becomes the Demand Volume ruler
ceiling. So the column now holds a JSONB LIST of candidate markets, not one.

Revision ID: 018
Revises: 017
Create Date: 2026-07-22

"""

from collections.abc import Sequence

from alembic import op

revision: str = "018"
down_revision: str | None = "017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("product_catalog", "reference_market", new_column_name="reference_candidates")


def downgrade() -> None:
    op.alter_column("product_catalog", "reference_candidates", new_column_name="reference_market")
