"""countries: add continent, group + prune to the AVAILABLE basket

2026-07-27. The `countries` table now holds ONLY the markets available for
analysis — the ones Google Ads can measure (see docs/paises-disponiveis.md).
Removes:
- RUS (Russia): DataForSEO unsupported (sanctions).
- CHN (China): Google blocked in mainland China (Baidu dominates) — no
  representative Google Ads search volume.
- KOR (South Korea): Naver holds ~55-60% of search, so Google Ads volume
  materially understates real demand — excluded until a Naver source is added.

Adds a `continent` column so the frontend groups the picker by continent and
lists countries alphabetically within each. No table FK references countries,
so pruning rows leaves historical indicator_values/mos_results untouched.

Revision ID: 023
Revises: 022
Create Date: 2026-07-27

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "023"
down_revision: str | None = "022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONTINENT: dict[str, str] = {
    "ARG": "Americas", "BRA": "Americas", "CAN": "Americas", "CHL": "Americas",
    "COL": "Americas", "MEX": "Americas", "PER": "Americas", "USA": "Americas",
    "DEU": "Europe", "ESP": "Europe", "FRA": "Europe", "GBR": "Europe",
    "ITA": "Europe", "NLD": "Europe", "POL": "Europe", "PRT": "Europe", "SWE": "Europe",
    "IDN": "Asia", "IND": "Asia", "JPN": "Asia", "SAU": "Asia", "TUR": "Asia",
    "ZAF": "Africa",
    "AUS": "Oceania",
}  # fmt: skip

_REMOVED = ("RUS", "CHN", "KOR")

_countries = sa.table(
    "countries",
    sa.column("iso3", sa.String),
    sa.column("name", sa.String),
    sa.column("iso2", sa.String),
    sa.column("un_numeric_code", sa.String),
    sa.column("search_language", sa.String),
    sa.column("continent", sa.String),
)


def upgrade() -> None:
    op.add_column("countries", sa.Column("continent", sa.String(), nullable=True))
    for iso3, continent in _CONTINENT.items():
        op.execute(_countries.update().where(_countries.c.iso3 == iso3).values(continent=continent))
    op.execute(_countries.delete().where(_countries.c.iso3.in_(_REMOVED)))


def downgrade() -> None:
    # Restore the pruned rows (best-effort — enough for the reference basket).
    op.bulk_insert(
        _countries,
        [
            {"iso3": "CHN", "name": "China", "iso2": "CN", "un_numeric_code": "156", "search_language": "zh", "continent": None},
            {"iso3": "RUS", "name": "Russia", "iso2": "RU", "un_numeric_code": "643", "search_language": "ru", "continent": None},
            {"iso3": "KOR", "name": "South Korea", "iso2": "KR", "un_numeric_code": "410", "search_language": "ko", "continent": None},
        ],
    )
    op.drop_column("countries", "continent")
