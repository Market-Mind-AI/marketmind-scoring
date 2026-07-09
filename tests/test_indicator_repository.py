import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.indicator_repository import IndicatorRepository


async def test_upsert_value_creates_then_updates(db_session: AsyncSession) -> None:
    repo = IndicatorRepository(db_session)
    indicator_code = f"trend_score-{uuid.uuid4()}"

    created = await repo.upsert_value(
        entity_key=None, country="USA", indicator_code=indicator_code, period_type="month", period_key="2026-07",
        source="pytrends", raw_value=60.11, normalized_score=60.11, direction="direct",
        calculation_window="2026-07", availability_status="available", notes="n=92",
    )  # fmt: skip
    assert created.raw_value == Decimal("60.11")

    updated = await repo.upsert_value(
        entity_key=None, country="USA", indicator_code=indicator_code, period_type="month", period_key="2026-07",
        source="pytrends", raw_value=70.0, normalized_score=70.0, direction="direct",
        calculation_window="2026-07", availability_status="available", notes="n=100",
    )  # fmt: skip
    assert updated.id == created.id
    assert updated.raw_value == Decimal("70.0")
    await db_session.commit()


async def test_upsert_reference_creates_then_updates_when_open(db_session: AsyncSession) -> None:
    repo = IndicatorRepository(db_session)
    indicator_code = f"gdp_per_capita_score-{uuid.uuid4()}"

    created = await repo.upsert_reference(
        indicator_code=indicator_code, period_key="2026-07", min_x=1000, max_x=90000,
        reference_population_size=27, is_closed=False,
    )  # fmt: skip
    assert float(created.min_x) == 1000

    updated = await repo.upsert_reference(
        indicator_code=indicator_code, period_key="2026-07", min_x=1200, max_x=95000,
        reference_population_size=28, is_closed=False,
    )  # fmt: skip
    assert updated.id == created.id
    assert float(updated.min_x) == 1200
    await db_session.commit()


async def test_upsert_reference_never_overwrites_closed_period(db_session: AsyncSession) -> None:
    repo = IndicatorRepository(db_session)
    indicator_code = f"gdp_growth_score-{uuid.uuid4()}"

    closed = await repo.upsert_reference(
        indicator_code=indicator_code, period_key="2026-06", min_x=-5, max_x=10,
        reference_population_size=27, is_closed=True,
    )  # fmt: skip
    await db_session.commit()

    attempted_update = await repo.upsert_reference(
        indicator_code=indicator_code, period_key="2026-06", min_x=-99, max_x=99,
        reference_population_size=27, is_closed=False,
    )  # fmt: skip
    assert attempted_update.id == closed.id
    assert float(attempted_update.min_x) == -5  # unchanged
    assert float(attempted_update.max_x) == 10  # unchanged
    await db_session.commit()


async def test_widen_reference_bounds_expands_and_ignores_within_range(db_session: AsyncSession) -> None:
    repo = IndicatorRepository(db_session)
    indicator_code = f"gdp_per_capita_score-{uuid.uuid4()}"
    await repo.upsert_reference(
        indicator_code=indicator_code, period_key="FIXED", min_x=500, max_x=100000,
        reference_population_size=0, is_closed=True,  # closed on purpose — widen must ignore this flag
    )  # fmt: skip
    await db_session.commit()

    # A value above the current max widens max (ignoring is_closed).
    widened = await repo.widen_reference_bounds(indicator_code, "FIXED", 120000.0)
    assert widened is not None
    assert float(widened.max_x) == 120000.0
    assert float(widened.min_x) == 500  # unchanged

    # A value below the current min widens min.
    widened = await repo.widen_reference_bounds(indicator_code, "FIXED", 100.0)
    assert widened is not None
    assert float(widened.min_x) == 100.0

    # A value already inside the range leaves both bounds untouched.
    widened = await repo.widen_reference_bounds(indicator_code, "FIXED", 50000.0)
    assert widened is not None
    assert float(widened.min_x) == 100.0
    assert float(widened.max_x) == 120000.0
    await db_session.commit()


async def test_widen_reference_bounds_returns_none_when_no_row(db_session: AsyncSession) -> None:
    repo = IndicatorRepository(db_session)
    result = await repo.widen_reference_bounds(f"missing-{uuid.uuid4()}", "FIXED", 42.0)
    assert result is None
