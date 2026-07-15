"""Curated per-country market terms (localized_markets)

Demand redesign (2026-07-15): the cross-country demand LEVEL no longer comes
from Google Trends (a structurally RELATIVE signal that could not be compared
across countries without a fragile shared "worldwide" term). It now comes from
Google Ads ABSOLUTE monthly search volume (via DataForSEO), summed over each
country's own approved search terms — real counts, directly comparable, no
normalization. Google Trends is removed from the MOS entirely.

The terms are curated by the identify-image LLM (image + target countries) and
reviewable by the user, replacing the old {lang: [names]} `localized_terms`
with a richer per-COUNTRY structure:

    localized_markets = {
      "<ISO3>": {
        "primary_name": str,
        "formal_name": str | null,
        "search_terms": [str],      # generous candidate list; level = SUM of
                                    # their absolute volumes (deduped)
        "excluded_terms": [str],    # disambiguation (e.g. "yerba buena"=mint)
        "confidence": float
      }, ...
    }

countries.search_language is kept — it is now the DataForSEO language_code for
each country's Google Ads calls.

Revision ID: 013
Revises: 012
Create Date: 2026-07-15

"""

import math
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "013"
down_revision: str | None = "012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FIXED_BOUNDS_PERIOD_KEY = "FIXED"
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


def upgrade() -> None:
    op.add_column("product_catalog", sa.Column("localized_markets", JSONB(), nullable=True))
    # Obsolete: the {lang: [names]} shape fed the removed Google Trends
    # multi-term comparison. Old data is not migratable to the new per-country
    # curated structure (re-run identify-image), so drop it.
    op.drop_column("product_catalog", "localized_terms")

    # trend_score changed from a 0-100 Trends passthrough to the Google Ads
    # demand LEVEL = ln(SUM of monthly search volume), so it now needs a FIXED
    # normalization bound in LN space (logarithmic normalization, same as
    # population_scale — a big market scores higher without crushing a small
    # strong one). Bounds: ln(1,000) .. ln(10,000,000) monthly searches.
    # Self-widens, never narrows. DIRECT.
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
                "min_x": math.log(1_000),
                "max_x": math.log(10_000_000),
                "reference_population_size": 0,
                "is_closed": False,
                "computed_at": datetime.now(UTC),
            }
        ],
    )


def downgrade() -> None:
    op.execute(
        _normalization_reference_table.delete().where(
            (_normalization_reference_table.c.period_key == _FIXED_BOUNDS_PERIOD_KEY)
            & (_normalization_reference_table.c.indicator_code == "trend_score")
        )
    )
    op.add_column("product_catalog", sa.Column("localized_terms", JSONB(), nullable=True))
    op.drop_column("product_catalog", "localized_markets")
