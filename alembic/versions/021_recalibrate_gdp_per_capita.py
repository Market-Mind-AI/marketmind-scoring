"""Recalibrate gdp_per_capita bound to an economic range + stop self-widening

2026-07-27. gdp_per_capita_score was the last macro indicator still
self-widening: its ceiling had drifted to ~90,026 (a very rich country). Now
fixed to [1000, 120000] USD — covers essentially every country (the richest,
Luxembourg/Qatar-scale, saturate at 100; the poorest near 0) — and added to
normalization._NO_SELF_WIDEN so a future ultra-rich outlier saturates instead of
stretching the ruler. Existing scores are recomputed against the new bound
outside this migration (they had drifted onto different historical rulers).

Revision ID: 021
Revises: 020
Create Date: 2026-07-27

"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "021"
down_revision: str | None = "020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FIXED = "FIXED"
_CODE = "gdp_per_capita_score"
_NEW = (1000.0, 120000.0)
_OLD = (500.0, 90026.5163005744)

_table = sa.table(
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


def _reseed(min_x: float, max_x: float) -> None:
    op.execute(_table.delete().where((_table.c.period_key == _FIXED) & (_table.c.indicator_code == _CODE)))
    op.bulk_insert(
        _table,
        [
            {
                "id": uuid.uuid4(),
                "indicator_code": _CODE,
                "period_key": _FIXED,
                "min_x": min_x,
                "max_x": max_x,
                "reference_population_size": 0,
                "is_closed": False,
                "computed_at": datetime.now(UTC),
            }
        ],
    )


def upgrade() -> None:
    _reseed(*_NEW)


def downgrade() -> None:
    _reseed(*_OLD)
