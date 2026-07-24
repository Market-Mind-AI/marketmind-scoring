"""Recalibrate macro normalization bounds to economically-anchored ranges

2026-07-24. Three MINMAX_FIXED indicators had bounds that self-widened to real
extreme outliers and then never narrowed, destroying discrimination:

- worldbank_inflation_score: max self-widened to 219.88% (Argentina's real
  hyperinflation, via the yerba-mate analysis). On a linear inverse ruler that
  crushed every normal economy (2–5%) into ~96, all "identical". New range
  [0, 20]% (inverse): ~1–2% lands near 100, and anything ≥20% (incl. Argentina)
  saturates at the worst score (0) instead of stretching the ruler for everyone.
- real_estate_growth_score: [-30, 60] compressed realistic house-price growth
  (−2%..+3%) into ~31–37. New [-10, 15].
- demand_stability_score: max widened to ~3.79 (a volatile product) compressed
  stable products into ~92–97. New [0, 1.0] (inverse coefficient of variation).

Combined with normalization._NO_SELF_WIDEN (these three are now added there),
the ruler stays a fixed, comparable, economically-sane yardstick: extremes
saturate, they no longer hijack the scale. Existing scores are recomputed
against the new bounds outside this migration.

Revision ID: 020
Revises: 019
Create Date: 2026-07-24

"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "020"
down_revision: str | None = "019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FIXED = "FIXED"

# indicator_code -> (min_x, max_x) after recalibration
_NEW_BOUNDS: dict[str, tuple[float, float]] = {
    "worldbank_inflation_score": (0.0, 20.0),
    "real_estate_growth_score": (-10.0, 15.0),
    "demand_stability_score": (0.0, 1.0),
}

# Prior (self-widened) values, restored on downgrade.
_OLD_BOUNDS: dict[str, tuple[float, float]] = {
    "worldbank_inflation_score": (-5.0, 219.883929014578),
    "real_estate_growth_score": (-30.0, 60.0),
    "demand_stability_score": (0.0, 3.7948840088992126),
}

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


def _reseed(bounds: dict[str, tuple[float, float]]) -> None:
    for code, (min_x, max_x) in bounds.items():
        op.execute(
            _table.delete().where((_table.c.period_key == _FIXED) & (_table.c.indicator_code == code))
        )
        op.bulk_insert(
            _table,
            [
                {
                    "id": uuid.uuid4(),
                    "indicator_code": code,
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
    _reseed(_NEW_BOUNDS)


def downgrade() -> None:
    _reseed(_OLD_BOUNDS)
