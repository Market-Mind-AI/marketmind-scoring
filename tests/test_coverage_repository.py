import uuid
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.coverage_repository import CoverageRepository
from app.utils.time import utc_now


async def test_get_returns_none_when_no_row(db_session: AsyncSession) -> None:
    repo = CoverageRepository(db_session)
    row = await repo.get("worldbank", None, "USA", f"unknown-{uuid.uuid4()}", "2026")
    assert row is None


async def test_is_fresh_rules() -> None:
    from app.db.models.coverage_index import CoverageIndex

    closed = CoverageIndex(
        id=uuid.uuid4(),
        source="worldbank",
        country="USA",
        indicator_code="x",
        period_key="2026",
        is_closed=True,
        last_fetched_at=utc_now() - timedelta(days=9999),
        ttl_days=None,
    )
    assert CoverageRepository.is_fresh(closed) is True

    open_no_ttl = CoverageIndex(
        id=uuid.uuid4(),
        source="worldbank",
        country="USA",
        indicator_code="x",
        period_key="2026-07",
        is_closed=False,
        last_fetched_at=utc_now(),
        ttl_days=None,
    )
    assert CoverageRepository.is_fresh(open_no_ttl) is False

    open_within_ttl = CoverageIndex(
        id=uuid.uuid4(),
        source="worldbank",
        country="USA",
        indicator_code="x",
        period_key="2026-07",
        is_closed=False,
        last_fetched_at=utc_now(),
        ttl_days=30,
    )
    assert CoverageRepository.is_fresh(open_within_ttl) is True

    open_expired_ttl = CoverageIndex(
        id=uuid.uuid4(),
        source="worldbank",
        country="USA",
        indicator_code="x",
        period_key="2026-01",
        is_closed=False,
        last_fetched_at=utc_now() - timedelta(days=31),
        ttl_days=30,
    )
    assert CoverageRepository.is_fresh(open_expired_ttl) is False

    assert CoverageRepository.is_fresh(None) is False


async def test_upsert_collected_creates_then_updates(db_session: AsyncSession) -> None:
    repo = CoverageRepository(db_session)
    indicator_code = f"gdp-{uuid.uuid4()}"

    created = await repo.upsert_collected(
        source="worldbank",
        entity_key=None,
        country="USA",
        indicator_code=indicator_code,
        period_key="2026",
        is_closed=False,
        ttl_days=30,
    )
    assert created.is_closed is False
    first_fetched_at = created.last_fetched_at

    updated = await repo.upsert_collected(
        source="worldbank",
        entity_key=None,
        country="USA",
        indicator_code=indicator_code,
        period_key="2026",
        is_closed=True,
        ttl_days=None,
    )
    assert updated.id == created.id
    assert updated.is_closed is True
    assert updated.last_fetched_at >= first_fetched_at

    await db_session.commit()
