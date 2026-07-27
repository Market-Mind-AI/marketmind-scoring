"""Reseed mobile_connectivity_score bound to a clean fixed ceiling

2026-07-27. mobile_connectivity_score (mobile subscriptions per 100 people) had
self-widened to 178.43 and, because past scores were never recomputed, came out
non-monotonic across countries (a stale-ruler discrepancy). Reseed to a clean
[0, 200] — 200 subs/100 comfortably covers real high-penetration markets (Gulf/
Asia), extremes saturate at 100 — and it is now in normalization._NO_SELF_WIDEN
(never widens again). Existing scores are recomputed against the new bound
outside this migration. The other remaining fixed indicators keep their sane
seeded bounds; they are only frozen (code), not reseeded.

Revision ID: 022
Revises: 021
Create Date: 2026-07-27

"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "022"
down_revision: str | None = "021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FIXED = "FIXED"
_CODE = "mobile_connectivity_score"
_NEW = (0.0, 200.0)
_OLD = (0.0, 178.4297448)

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
