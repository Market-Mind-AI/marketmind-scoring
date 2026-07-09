"""Seed fixed normalization bounds for the 12 remaining scored indicators

2026-07-08: MINMAX_REFERENCE (percentile bounds over a reference country
basket) was removed — every scored indicator now normalizes against a
fixed, absolute bound (period_key="FIXED"), the same mechanism migration
005 already used for growth_score/demand_stability_score. These initial
bounds are plausible starting points researched from each indicator's real
formula, NOT hard limits: indicator_repository.widen_reference_bounds
auto-widens (never narrows) any bound the first time a real observed value
falls outside it, so a market is never clipped at a guessed edge — see
app/services/indicators/normalization.py.

is_closed=False on purpose (unlike 005's fixed bounds): widen_reference_bounds
ignores is_closed anyway, but leaving it open also documents that these are
expected to move as real data arrives.

Revision ID: 010
Revises: 009
Create Date: 2026-07-08

"""

import math
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "010"
down_revision: str | None = "009"
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

# (indicator_code, min_x, max_x) — see the plan/normalization.py for the
# per-indicator rationale. growth_score/demand_stability_score are NOT here
# (already seeded by migration 005).
_FIXED_BOUNDS: list[tuple[str, float, float]] = [
    ("gdp_per_capita_score", 500.0, 100000.0),
    ("gdp_growth_score", -10.0, 10.0),
    ("population_scale_score", math.log(1_000_000), math.log(1_500_000_000)),
    ("mobile_connectivity_score", 0.0, 150.0),
    ("worldbank_inflation_score", -5.0, 50.0),
    ("population_growth_score", -3.0, 5.0),
    ("net_migration_rate_score", -15.0, 15.0),
    ("consumer_spending_score", 30.0, 85.0),
    ("investment_score", 15.0, 45.0),
    ("net_exports_score", -15.0, 15.0),
    ("trade_openness_score", 20.0, 200.0),
    ("real_estate_growth_score", -30.0, 60.0),
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
                "reference_population_size": 0,  # not empirical — theoretical starting range
                "is_closed": False,  # self-widens as real data arrives
                "computed_at": now,
            }
            for indicator_code, min_x, max_x in _FIXED_BOUNDS
        ],
    )


def downgrade() -> None:
    codes = [code for code, _, _ in _FIXED_BOUNDS]
    op.execute(
        _normalization_reference_table.delete().where(
            sa.and_(
                _normalization_reference_table.c.period_key == FIXED_BOUNDS_PERIOD_KEY,
                _normalization_reference_table.c.indicator_code.in_(codes),
            )
        )
    )
