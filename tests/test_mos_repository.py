import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.mos_repository import MosRepository
from app.repositories.product_repository import ProductRepository


def _unique_period_key() -> str:
    return f"2026-07-test-{uuid.uuid4().hex[:8]}"


async def test_upsert_weight_creates_then_updates(db_session: AsyncSession) -> None:
    repo = MosRepository(db_session)
    period_key = _unique_period_key()

    created = await repo.upsert_weight(
        indicator_code="demand", dimension="demand", period_key=period_key, weight=0.3, is_closed=False
    )
    await db_session.commit()
    assert float(created.weight) == 0.3
    assert created.is_closed is False

    updated = await repo.upsert_weight(
        indicator_code="demand", dimension="demand", period_key=period_key, weight=0.4, is_closed=False
    )
    await db_session.commit()
    assert updated.id == created.id
    assert float(updated.weight) == 0.4


async def test_upsert_weight_never_overwrites_closed_period(db_session: AsyncSession) -> None:
    repo = MosRepository(db_session)
    period_key = _unique_period_key()

    await repo.upsert_weight(
        indicator_code="market_size", dimension="market_size", period_key=period_key, weight=0.25, is_closed=True
    )
    await db_session.commit()

    result = await repo.upsert_weight(
        indicator_code="market_size", dimension="market_size", period_key=period_key, weight=0.99, is_closed=False
    )
    await db_session.commit()

    assert float(result.weight) == 0.25
    assert result.is_closed is True


async def test_upsert_mos_result_creates_then_updates(db_session: AsyncSession) -> None:
    entity = await ProductRepository(db_session).create_entity(canonical_title=f"Product {uuid.uuid4()}")
    await db_session.commit()

    repo = MosRepository(db_session)
    period_key = _unique_period_key()

    created = await repo.upsert_mos_result(
        entity_key=entity.entity_key, country="USA", period_type="month", period_key=period_key,
        mos=70.0, mos_before_cap=70.0, capped_by=None, topsis_score=0.7,
        availability_status="available", excluded_reason=None,
    )  # fmt: skip
    await db_session.commit()
    assert float(created.mos or 0) == 70.0
    assert created.library_topsis_score is None
    assert created.library_mos is None

    updated = await repo.upsert_mos_result(
        entity_key=entity.entity_key, country="USA", period_type="month", period_key=period_key,
        mos=39.0, mos_before_cap=70.0, capped_by="trend_score_cap", topsis_score=0.7,
        library_topsis_score=0.65, library_mos=65.0,
        availability_status="available", excluded_reason=None,
    )  # fmt: skip
    await db_session.commit()

    assert updated.id == created.id
    assert float(updated.mos or 0) == 39.0
    assert updated.capped_by == "trend_score_cap"
    assert float(updated.library_topsis_score or 0) == 0.65
    assert float(updated.library_mos or 0) == 65.0
