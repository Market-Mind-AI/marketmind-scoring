"""Seed reference countries

Seeds the canonical `countries` table with a starter basket used both as the
cold-start reference population for Entropy Weight calibration (see memory
marketmind_mos_engine, item 7) and as the minimum set needed to exercise
check_or_collect end-to-end (country code translation per source — see
app/repositories/country_repository.py). Covers G20 members plus a few extra
Iberian/Nordic countries. Not a full ISO 3166 list — extending coverage to
the full catalog of countries is a follow-up, not blocking for Fase 1.

worldbank_country_id and oecd_ref_area are left NULL on purpose: both
sources accept ISO3 directly for the vast majority of countries, so
app/repositories/country_repository.py falls back to `iso3` when unset.
Only undata_location_id (UN M49 numeric code) has no such fallback and is
seeded explicitly.

Revision ID: 003
Revises: 002
Create Date: 2026-07-02

"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "003"
down_revision: str | None = "002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (iso3, name, iso2, un_m49_numeric)
_COUNTRIES: list[tuple[str, str, str, str]] = [
    ("USA", "United States", "US", "840"),
    ("BRA", "Brazil", "BR", "076"),
    ("DEU", "Germany", "DE", "276"),
    ("FRA", "France", "FR", "250"),
    ("GBR", "United Kingdom", "GB", "826"),
    ("ITA", "Italy", "IT", "380"),
    ("ESP", "Spain", "ES", "724"),
    ("CAN", "Canada", "CA", "124"),
    ("MEX", "Mexico", "MX", "484"),
    ("ARG", "Argentina", "AR", "032"),
    ("CHL", "Chile", "CL", "152"),
    ("COL", "Colombia", "CO", "170"),
    ("PER", "Peru", "PE", "604"),
    ("JPN", "Japan", "JP", "392"),
    ("CHN", "China", "CN", "156"),
    ("IND", "India", "IN", "356"),
    ("KOR", "South Korea", "KR", "410"),
    ("AUS", "Australia", "AU", "036"),
    ("RUS", "Russia", "RU", "643"),
    ("ZAF", "South Africa", "ZA", "710"),
    ("IDN", "Indonesia", "ID", "360"),
    ("TUR", "Turkey", "TR", "792"),
    ("SAU", "Saudi Arabia", "SA", "682"),
    ("NLD", "Netherlands", "NL", "528"),
    ("PRT", "Portugal", "PT", "620"),
    ("POL", "Poland", "PL", "616"),
    ("SWE", "Sweden", "SE", "752"),
]

_countries_table = sa.table(
    "countries",
    sa.column("iso3", sa.String),
    sa.column("name", sa.String),
    sa.column("iso2", sa.String),
    sa.column("un_numeric_code", sa.String),
    sa.column("undata_location_id", sa.String),
    sa.column("created_at", sa.DateTime),
)


def upgrade() -> None:
    now = datetime.now(UTC)
    op.bulk_insert(
        _countries_table,
        [
            {
                "iso3": iso3,
                "name": name,
                "iso2": iso2,
                "un_numeric_code": un_code,
                "undata_location_id": un_code,
                "created_at": now,
            }
            for iso3, name, iso2, un_code in _COUNTRIES
        ],
    )


def downgrade() -> None:
    iso3_codes = [c[0] for c in _COUNTRIES]
    op.execute(_countries_table.delete().where(_countries_table.c.iso3.in_(iso3_codes)))
