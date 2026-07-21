"""Demand Volume: linear fixed ceiling for trend_score (replaces the ln bound)

2026-07-16 redesign. trend_score is the DEMAND VOLUME level = absolute monthly
Google Ads search volume for the product's approved terms. Its raw value is now
the absolute count (not ln), normalized LINEARLY so the score is PROPORTIONAL to
volume — sales potential scales linearly with the number of interested buyers
(no diminishing returns), so a market with 2x the searches scores ~2x. The
earlier ln transform compressed that (an 8x volume gap became ~1.4x of score),
letting a tiny high-quality market tie a huge one.

Bound: [0, 2_000_000] monthly searches. 2M is a documented scale anchor for
"maximal addressable national demand" for a mainstream consumer product (e.g.
air fryer's largest national market, Brazil, is ~1.5M, comfortably below, so
strong markets land in the upper half of 0-100 without saturating). Unlike the
other fixed bounds this one does NOT self-widen (see normalization._NO_SELF_WIDEN):
markets above 2M saturate at 100, keeping the volume scale stable across
products (a single mega-volume product must not retroactively shrink every other
product's demand score).

Revision ID: 014
Revises: 013
Create Date: 2026-07-16

"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "014"
down_revision: str | None = "013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FIXED_BOUNDS_PERIOD_KEY = "FIXED"
_TREND_MAX = 2_000_000.0

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


def _reseed(min_x: float, max_x: float) -> None:
    op.execute(
        _normalization_reference_table.delete().where(
            (_normalization_reference_table.c.period_key == _FIXED_BOUNDS_PERIOD_KEY)
            & (_normalization_reference_table.c.indicator_code == "trend_score")
        )
    )
    op.bulk_insert(
        _normalization_reference_table,
        [
            {
                "id": uuid.uuid4(),
                "indicator_code": "trend_score",
                "period_key": _FIXED_BOUNDS_PERIOD_KEY,
                "min_x": min_x,
                "max_x": max_x,
                "reference_population_size": 0,
                "is_closed": False,
                "computed_at": datetime.now(UTC),
            }
        ],
    )


def upgrade() -> None:
    _reseed(0.0, _TREND_MAX)


def downgrade() -> None:
    import math

    _reseed(math.log(1_000), math.log(10_000_000))
