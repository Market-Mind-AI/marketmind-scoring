"""Localized search terms per country language

Keyword localization (2026-07-10): Google Trends measures the share of a
country's searches devoted to an *exact* term, so a single Spanish keyword
("jamón ibérico") structurally understates Portugal, where buyers search
"presunto ibérico" — a language artifact, not a demand gap, that capped
Portugal's trend_score below the non-compensatory floor. Fix: each country
is measured with the term in ITS OWN language, all localized terms compared
on one common Google Trends axis (see marketmind_mos_engine).

Two schema additions:
  - product_catalog.localized_terms (JSONB): {lang: native_term} produced by
    the identify-image LLM (e.g. {"es":"jamón ibérico","pt":"presunto
    ibérico","en":"iberico ham"}). Written only when a NEW entity is created.
  - countries.search_language (str): the primary search language per country,
    seeded here for the reference basket. compute_trend_score reads the
    interest_by_country row for (country, this language's term).

Revision ID: 011
Revises: 010
Create Date: 2026-07-10

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "011"
down_revision: str | None = "010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# ISO3 -> primary Google-search language (the term a local buyer actually
# types). Reference basket only (matches migration 003); ~14 distinct
# languages across the 27 countries.
_SEARCH_LANGUAGE_BY_ISO3: dict[str, str] = {
    "USA": "en", "GBR": "en", "CAN": "en", "AUS": "en", "IND": "en", "ZAF": "en",
    "ESP": "es", "MEX": "es", "ARG": "es", "CHL": "es", "COL": "es", "PER": "es",
    "BRA": "pt", "PRT": "pt",
    "DEU": "de", "FRA": "fr", "ITA": "it", "JPN": "ja", "CHN": "zh", "KOR": "ko",
    "RUS": "ru", "IDN": "id", "TUR": "tr", "SAU": "ar", "NLD": "nl", "POL": "pl",
    "SWE": "sv",
}  # fmt: skip


def upgrade() -> None:
    op.add_column("product_catalog", sa.Column("localized_terms", JSONB(), nullable=True))
    op.add_column("countries", sa.Column("search_language", sa.String(), nullable=True))

    countries = sa.table("countries", sa.column("iso3", sa.String), sa.column("search_language", sa.String))
    for iso3, language in _SEARCH_LANGUAGE_BY_ISO3.items():
        op.execute(countries.update().where(countries.c.iso3 == iso3).values(search_language=language))


def downgrade() -> None:
    op.drop_column("countries", "search_language")
    op.drop_column("product_catalog", "localized_terms")
