"""Recalibrate the macro rulers against the now-complete reference population

2026-08-27. The fixed bounds of the country-level indicators were seeded as
"plausible starting points, not perfectly researched limits" (see
normalization.py) back when almost no country had macro data — the reference
population simply did not exist yet, which is why the percentile-based reference
of Cap. 4 §4.1 was abandoned in 2026-07-08 ("a percentile-based reference
population never populates organically without forcing every analysis to cover
all reference countries").

That premise expired. All 24 supported countries now carry every macro
indicator, so the reference population is COMPLETE — not a sample, the whole
basket. Measured against it, the seeded rulers were badly centred:

    indicator                 ruler        real spread        ruler used
    population_growth      -3 .. 5      -0.82 ..  1.74           32%
    gdp_growth            -10 .. 10     -0.38 ..  7.29           38%
    net_migration_rate    -15 .. 15     -5.02 ..  8.20           44%
    mobile_connectivity     0 .. 200    79.35 .. 179.3           50%
    consumer_spending      30 .. 85     43.09 ..  73.19          55%
    net_exports           -15 .. 15     -5.40 .. 11.22           55%
    investment             15 .. 45     13.88 ..  31.68          59%  (ZAF at 0)
    gdp_per_capita       1000 .. 120k    2702 ..  90.0k          73%
    worldbank_inflation     0 .. 20      2.03 .. 141.9           22 of 24 in 2..8

Half of each ruler was dead space, and dead space is not neutral: it compresses
every country into a narrow band of the 0-100 scale, so an indicator that DOES
separate markets stops separating them. Population Growth is the clearest case —
a genuine spread from Poland shrinking 0.8%/yr to Saudi Arabia growing 1.7%/yr
came out as scores 27 to 59, and the whole Demographic Sustainability dimension
inherited that flatness. Investment was worse than flat: South Africa's 13.9%
fell BELOW the floor and scored 0, reading as "no investment at all".

New bounds are the observed spread of the basket rounded outward to the nearest
economically legible number, never fitted tightly to today's values. Measured
effect on the per-indicator score dispersion (standard deviation across the 24
countries): population_growth 7.6 -> 20.2, net_migration 9.8 -> 19.5,
gdp_growth 8.2 -> 16.5, mobile_connectivity 12.4 -> 20.7, investment 16.0 ->
21.2 with nobody unfairly pinned at 0.

Inflation is the one bound narrowed for a different reason. Its ruler is not a
spread, it is a threshold: 22 of the 24 countries live between 2% and 8%, and
the two crisis economies (Turkey 49%, Argentina 142%) are meant to SATURATE at
the worst score rather than stretch the yardstick for everyone — the lesson of
2026-07-24. A ceiling of 20% put every normal economy in the top third of the
scale (median score 80); 12% keeps the saturation and gives the normal range
the room it needs.

NOT touched, and why:
  - population_scale, growth_score, demand_stability: already occupy 67-99% of
    their rulers.
  - trade_openness: migration 024 set it deliberately so the Netherlands (150%,
    an entrepôt outlier) saturates instead of crushing everyone else.
  - real_estate_growth: both tails are genuine extremes (Turkey -12%, Portugal
    +25%) and the indicator is not mandatory.
  - trend_score: its real ruler is per-product (the reference market's volume,
    Cap. 4 §4.2.1); the row here is only the fallback ceiling.

Bounds stay FIXED and frozen (normalization._NO_SELF_WIDEN): a country added to
the basket later saturates at 0/100 rather than widening the ruler and
retroactively rescoring everyone. Existing scores are re-normalized from
raw_value the next time each analysis runs (indicator_engine.compute_all), so
this is retroactive by design — every country ends up on ONE yardstick.

Revision ID: 030
Revises: 029
Create Date: 2026-08-27

"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "030"
down_revision: str | None = "029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FIXED = "FIXED"

# indicator_code -> (new bounds, old bounds)
_BOUNDS: dict[str, tuple[tuple[float, float], tuple[float, float]]] = {
    "population_growth_score": ((-1.0, 2.0), (-3.0, 5.0)),
    "gdp_growth_score": ((-2.0, 8.0), (-10.0, 10.0)),
    "gdp_per_capita_score": ((2000.0, 95000.0), (1000.0, 120000.0)),
    "investment_score": ((12.0, 35.0), (15.0, 45.0)),
    "mobile_connectivity_score": ((70.0, 190.0), (0.0, 200.0)),
    "net_exports_score": ((-8.0, 12.0), (-15.0, 15.0)),
    "net_migration_rate_score": ((-6.0, 9.0), (-15.0, 15.0)),
    "consumer_spending_score": ((40.0, 75.0), (30.0, 85.0)),
    "worldbank_inflation_score": ((0.0, 12.0), (0.0, 20.0)),
}

# The basket the bounds were measured against (see the module docstring).
_REFERENCE_POPULATION_SIZE = 24

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


def _reseed(which: int, population_size: int) -> None:
    codes = list(_BOUNDS)
    op.execute(_table.delete().where((_table.c.period_key == _FIXED) & (_table.c.indicator_code.in_(codes))))
    op.bulk_insert(
        _table,
        [
            {
                "id": uuid.uuid4(),
                "indicator_code": code,
                "period_key": _FIXED,
                "min_x": _BOUNDS[code][which][0],
                "max_x": _BOUNDS[code][which][1],
                "reference_population_size": population_size,
                "is_closed": False,
                "computed_at": datetime.now(UTC),
            }
            for code in codes
        ],
    )


def upgrade() -> None:
    _reseed(0, _REFERENCE_POPULATION_SIZE)


def downgrade() -> None:
    # The seeded rulers were never measured against a population, hence 0.
    _reseed(1, 0)
