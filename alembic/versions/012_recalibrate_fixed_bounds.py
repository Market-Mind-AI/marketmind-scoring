"""Recalibrate + clean the FIXED normalization bounds

Audit 2026-07-14: the dev DB's `normalization_reference` FIXED rows had drifted
to implausibly wide maxes (e.g. gdp_growth/population_growth/net_migration/
net_exports all at max 50 — impossible for those percentage indicators) plus 22
polluted `gdp_per_capita_score-<uuid>` rows. Root cause: the test suite runs
against the shared dev DB and self-widens (never narrows) these bounds with
synthetic values, and `normalization_reference` is preserved across test resets
(conftest) — so the pollution accumulated. Since widen_reference_bounds can only
WIDEN, this over-wide state can never self-correct; only an explicit overwrite
fixes it (test isolation, added separately, stops the recurrence).

This migration hard-resets ALL FIXED bounds to a single clean, realistic set
(restoring migration 010/005's mostly-fine values, tightening the few that were
genuinely seeded too wide — worldbank_inflation, demand_stability — and dropping
the polluted per-entity rows). widen_reference_bounds still auto-widens any of
these if a real observed value ever exceeds them.

Revision ID: 012
Revises: 011
Create Date: 2026-07-14

"""

import math
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "012"
down_revision: str | None = "011"
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

# Clean, realistic starting bounds for every scored (MINMAX_FIXED) indicator.
# Percentage/rate indicators use their real-world ranges (not placeholder 50s).
_FIXED_BOUNDS: list[tuple[str, float, float]] = [
    ("gdp_per_capita_score", 500.0, 90000.0),  # USD/capita
    ("gdp_growth_score", -10.0, 10.0),  # % annual
    ("population_scale_score", math.log(1_000_000), math.log(1_500_000_000)),  # ln(population)
    ("mobile_connectivity_score", 0.0, 150.0),  # subscriptions / 100 people
    ("worldbank_inflation_score", -5.0, 30.0),  # % annual (INVERSE); tightened from 50
    ("population_growth_score", -3.0, 5.0),  # % annual
    ("net_migration_rate_score", -15.0, 15.0),  # per 1000
    ("consumer_spending_score", 30.0, 85.0),  # % of GDP
    ("investment_score", 15.0, 45.0),  # % of GDP (gross fixed capital formation)
    ("net_exports_score", -15.0, 15.0),  # % of GDP
    ("trade_openness_score", 20.0, 200.0),  # (exports+imports) / GDP %
    ("real_estate_growth_score", -30.0, 60.0),  # % over the window
    ("growth_score", -50.0, 150.0),  # Google Trends growth %
    ("demand_stability_score", 0.0, 1.0),  # CV; reset from noise-widened 3.29
]


def _seed_clean_fixed_bounds() -> None:
    now = datetime.now(UTC)
    # Drop EVERY FIXED row (canonical, drifted, and polluted per-entity ones),
    # then insert the one clean set.
    op.execute(
        _normalization_reference_table.delete().where(
            _normalization_reference_table.c.period_key == FIXED_BOUNDS_PERIOD_KEY
        )
    )
    op.bulk_insert(
        _normalization_reference_table,
        [
            {
                "id": uuid.uuid4(),
                "indicator_code": indicator_code,
                "period_key": FIXED_BOUNDS_PERIOD_KEY,
                "min_x": min_x,
                "max_x": max_x,
                "reference_population_size": 0,
                "is_closed": False,
                "computed_at": now,
            }
            for indicator_code, min_x, max_x in _FIXED_BOUNDS
        ],
    )


def upgrade() -> None:
    _seed_clean_fixed_bounds()


def downgrade() -> None:
    # No true inverse (prior state was polluted); leave the clean set in place.
    pass
