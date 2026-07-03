"""Seed fixed normalization bounds (Fase 3)

Growth Score and Demand Stability (CV) are product-specific indicators with
no natural country universe to draw empirical percentile bounds from (Cap.
4 §4.2.2/4.2.3 + plan section 7) — they use fixed, documented theoretical
clipping ranges instead: GrowthRaw in [-50, 150] (%), CV in [0, 1]. Modeled
as normalization_reference rows with the sentinel period_key "FIXED"
(never tied to a calendar month, permanently closed) so the same lookup
path (indicator_code + period_key) works for every indicator — see
app/services/indicators/normalization.py.

Revision ID: 005
Revises: 004
Create Date: 2026-07-02

"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "005"
down_revision: str | None = "004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FIXED_BOUNDS_PERIOD_KEY = "FIXED"

_normalization_reference_table = sa.table(
    "normalization_reference",
    sa.column("id", postgresql.UUID(as_uuid=True)),
    sa.column("indicator_code", sa.String),
    sa.column("period_key", sa.String),
    sa.column("min_x", sa.Numeric),
    sa.column("max_x", sa.Numeric),
    sa.column("reference_population_size", sa.Integer),
    sa.column("is_closed", sa.Boolean),
    sa.column("computed_at", sa.DateTime(timezone=True)),
)

_FIXED_BOUNDS: list[tuple[str, float, float]] = [
    ("growth_score", -50.0, 150.0),
    ("demand_stability_score", 0.0, 1.0),
]


def upgrade() -> None:
    now = datetime.now(UTC)
    op.bulk_insert(
        _normalization_reference_table,
        [
            {
                "id": uuid.uuid4(),
                "indicator_code": indicator_code,
                "period_key": FIXED_BOUNDS_PERIOD_KEY,
                "min_x": min_x,
                "max_x": max_x,
                "reference_population_size": 0,  # theoretical, not empirical
                "is_closed": True,
                "computed_at": now,
            }
            for indicator_code, min_x, max_x in _FIXED_BOUNDS
        ],
    )


def downgrade() -> None:
    op.execute(
        _normalization_reference_table.delete().where(
            _normalization_reference_table.c.period_key == FIXED_BOUNDS_PERIOD_KEY
        )
    )
