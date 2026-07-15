import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import EntityNotFoundError
from app.domain.enums import IndicatorDimension
from app.repositories.indicator_repository import IndicatorRepository
from app.repositories.mos_repository import MosRepository
from app.repositories.product_repository import ProductRepository
from app.services.mos_engine import MosEngine

_COUNTRY_LEVEL_CODES = [
    "internet_penetration_score", "mobile_connectivity_score", "gdp_per_capita_score", "gdp_growth_score",
    "population_scale_score", "urbanization_score", "population_growth_score", "net_migration_rate_score",
    "worldbank_inflation_score",
    "consumer_spending_score", "investment_score", "net_exports_score", "trade_openness_score",
]  # fmt: skip

_PRODUCT_LEVEL_CODES = [
    "trend_score", "growth_score", "demand_stability_score", "regional_demand_score", "search_intent_score",
]  # fmt: skip


def _unique_period_key() -> str:
    return f"2026-07-test-{uuid.uuid4().hex[:8]}"


async def _seed_indicators(
    db_session: AsyncSession, *, entity_key: uuid.UUID, country: str, period_key: str, trend_score: float = 90.0,
    codes: list[str] | None = None,
) -> None:  # fmt: skip
    repo = IndicatorRepository(db_session)
    for code in _COUNTRY_LEVEL_CODES:
        if codes is not None and code not in codes:
            continue
        await repo.upsert_value(
            entity_key=None, country=country, indicator_code=code, period_type="month", period_key=period_key,
            source="worldbank", raw_value=1, normalized_score=90.0, direction="direct", calculation_window="test",
            availability_status="available", notes=None,
        )  # fmt: skip
    for code in _PRODUCT_LEVEL_CODES:
        if codes is not None and code not in codes:
            continue
        score = trend_score if code == "trend_score" else 90.0
        await repo.upsert_value(
            entity_key=entity_key, country=country, indicator_code=code, period_type="month", period_key=period_key,
            source="pytrends", raw_value=1, normalized_score=score, direction="direct", calculation_window="test",
            availability_status="available", notes=None,
        )  # fmt: skip
    await db_session.commit()


async def _seed_weights(db_session: AsyncSession, *, period_key: str) -> None:
    repo = MosRepository(db_session)
    # Uniform weight summing to 1 across however many dimensions exist —
    # with the fixed ideal=100/anti-ideal=0 TOPSIS vector, equal sub-scores
    # across all dimensions always yield topsis = value/100 as long as the
    # weights sum to 1, regardless of dimension count (see topsis.py).
    uniform_weight = 1.0 / len(list(IndicatorDimension))
    for dimension in IndicatorDimension:
        await repo.upsert_weight(
            indicator_code=dimension.value, dimension=dimension.value, period_key=period_key, weight=uniform_weight,
            is_closed=False,
        )  # fmt: skip
    await db_session.commit()


async def test_unknown_entity_raises(db_session: AsyncSession) -> None:
    engine = MosEngine(db_session)
    with pytest.raises(EntityNotFoundError):
        await engine.compute_mos(entity_key=uuid.uuid4(), countries=["USA"])


async def test_missing_mandatory_indicator_excludes_country(db_session: AsyncSession) -> None:
    entity = await ProductRepository(db_session).create_entity(canonical_title=f"Product {uuid.uuid4()}")
    await db_session.commit()
    period_key = _unique_period_key()
    # Deliberately omit gdp_per_capita_score.
    codes = [c for c in (_COUNTRY_LEVEL_CODES + _PRODUCT_LEVEL_CODES) if c != "gdp_per_capita_score"]
    await _seed_indicators(db_session, entity_key=entity.entity_key, country="USA", period_key=period_key, codes=codes)
    await _seed_weights(db_session, period_key=period_key)

    engine = MosEngine(db_session)
    results = await engine.compute_mos(entity_key=entity.entity_key, countries=["USA"], period_key=period_key)

    assert len(results) == 1
    result = results[0]
    assert result.mos is None
    assert result.availability_status == "unavailable"
    assert result.excluded_reason is not None
    assert "gdp_per_capita_score" in result.excluded_reason


async def test_missing_weight_excludes_country(db_session: AsyncSession) -> None:
    entity = await ProductRepository(db_session).create_entity(canonical_title=f"Product {uuid.uuid4()}")
    await db_session.commit()
    period_key = _unique_period_key()
    await _seed_indicators(db_session, entity_key=entity.entity_key, country="USA", period_key=period_key)
    # No weights seeded at all.

    engine = MosEngine(db_session)
    results = await engine.compute_mos(entity_key=entity.entity_key, countries=["USA"], period_key=period_key)

    assert results[0].mos is None
    assert results[0].excluded_reason is not None
    assert "No calibrated weight" in results[0].excluded_reason


async def test_low_trend_score_is_not_capped(db_session: AsyncSession) -> None:
    # No non-compensatory cap (user decision 2026-07-15): a low Demand signal
    # just lowers the DEMAND dimension mean; it never hard-caps the MOS.
    # DEMAND = mean(trend=10, growth/stability/intent=90) = 70, the other 5
    # dimensions = 90, uniform weights -> TOPSIS closeness 0.85063 -> MOS 85.06.
    entity = await ProductRepository(db_session).create_entity(canonical_title=f"Product {uuid.uuid4()}")
    await db_session.commit()
    period_key = _unique_period_key()
    await _seed_indicators(
        db_session, entity_key=entity.entity_key, country="USA", period_key=period_key, trend_score=10.0
    )
    await _seed_weights(db_session, period_key=period_key)

    engine = MosEngine(db_session)
    results = await engine.compute_mos(entity_key=entity.entity_key, countries=["USA"], period_key=period_key)

    result = results[0]
    assert result.availability_status == "available"
    assert result.capped_by is None
    assert float(result.mos or 0) == pytest.approx(85.0626, rel=1e-4)
    assert float(result.mos_before_cap or 0) == pytest.approx(float(result.mos or 0))


async def test_high_trend_score_has_no_cap(db_session: AsyncSession) -> None:
    entity = await ProductRepository(db_session).create_entity(canonical_title=f"Product {uuid.uuid4()}")
    await db_session.commit()
    period_key = _unique_period_key()
    await _seed_indicators(
        db_session, entity_key=entity.entity_key, country="USA", period_key=period_key, trend_score=90.0
    )
    await _seed_weights(db_session, period_key=period_key)

    engine = MosEngine(db_session)
    results = await engine.compute_mos(entity_key=entity.entity_key, countries=["USA"], period_key=period_key)

    result = results[0]
    assert result.capped_by is None
    assert float(result.mos or 0) == pytest.approx(90.0)
    assert float(result.mos_before_cap or 0) == pytest.approx(90.0)
    assert float(result.topsis_score or 0) == pytest.approx(0.9)


async def test_mos_is_independent_per_country(db_session: AsyncSession) -> None:
    entity = await ProductRepository(db_session).create_entity(canonical_title=f"Product {uuid.uuid4()}")
    await db_session.commit()
    period_key = _unique_period_key()
    await _seed_indicators(
        db_session, entity_key=entity.entity_key, country="USA", period_key=period_key, trend_score=90.0
    )
    await _seed_indicators(
        db_session, entity_key=entity.entity_key, country="BRA", period_key=period_key, trend_score=10.0
    )
    await _seed_weights(db_session, period_key=period_key)

    engine = MosEngine(db_session)
    results = await engine.compute_mos(entity_key=entity.entity_key, countries=["USA", "BRA"], period_key=period_key)

    by_country = {r.country: r for r in results}
    # Each country scores on its OWN Demand: USA (trend 90) -> all dims 90 -> 90;
    # BRA (trend 10) -> DEMAND mean 70, other dims 90 -> 85.06. No cap involved.
    assert float(by_country["USA"].mos or 0) == pytest.approx(90.0)
    assert float(by_country["BRA"].mos or 0) == pytest.approx(85.0626, rel=1e-4)
