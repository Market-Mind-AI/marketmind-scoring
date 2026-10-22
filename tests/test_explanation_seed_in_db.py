"""Every metric the code has a text for must have that text in the database.

The labels and phrases are read from `metric_explanations` and
`metric_band_phrases` at runtime; `explanation_seed.py` only holds what the
migrations insert. A metric added to the seed after its migration ran never
reaches the tables, and the report shows it without a name -- which is what
happened to broadband until migration 056.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.mos.explanation_seed import band_phrase_rows, metric_rows


async def test_every_seeded_metric_has_a_label_in_the_database(db_session: AsyncSession) -> None:
    in_db = {row[0] for row in (await db_session.execute(text("select metric_code from metric_explanations"))).all()}

    missing = {row["metric_code"] for row in metric_rows()} - in_db

    assert not missing, f"metrics with a text in the seed but no row in metric_explanations: {sorted(missing)}"


async def test_every_seeded_metric_has_its_band_phrases_in_the_database(db_session: AsyncSession) -> None:
    in_db = {
        row[0] for row in (await db_session.execute(text("select distinct metric_code from metric_band_phrases"))).all()
    }

    missing = {row["metric_code"] for row in band_phrase_rows()} - in_db

    assert not missing, f"metrics with band phrases in the seed but none in metric_band_phrases: {sorted(missing)}"
