"""Recalibrate the Trade Openness normalization ceiling

2026-07-27. trade_openness_score (World Bank NE.TRD.GNFS.ZS = (exports+imports)/
GDP, %) used a fixed ruler of [20, 200]. But a ceiling of 200% is only reached
by tiny entrepôt economies (Singapore ~320%, Luxembourg ~380%) that are NOT in
MarketMind's available-markets basket — within the basket the maximum is the
Netherlands at ~150%. The top ~25% of the ruler was dead space, so every country
was crushed to the low end (USA→2.8, Brazil→8.5), which read as "always too low".

New range [20, 130]: values above 130% saturate at 100 (NLD, genuinely
hyper-open, clips at the top) while the realistic 25–104% span of the basket is
spread across most of the 0–100 scale. The DATA was always correct — large
domestic economies (USA, Brazil) still score low because they genuinely trade
little relative to GDP; that is a real structural signal, not a ruler artifact.

trade_openness_score is already frozen in normalization._NO_SELF_WIDEN, so this
ceiling never self-widens. Existing scores are recomputed against the new bound
the next time each analysis runs (indicator_engine.compute_all re-normalizes
from raw every run, then compute_mos re-scores).

Revision ID: 024
Revises: 023
Create Date: 2026-07-27

"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "024"
down_revision: str | None = "023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FIXED = "FIXED"
_CODE = "trade_openness_score"
_NEW_BOUNDS = (20.0, 130.0)
_OLD_BOUNDS = (20.0, 200.0)

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


def _reseed(bounds: tuple[float, float]) -> None:
    min_x, max_x = bounds
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
    _reseed(_NEW_BOUNDS)


def downgrade() -> None:
    _reseed(_OLD_BOUNDS)
