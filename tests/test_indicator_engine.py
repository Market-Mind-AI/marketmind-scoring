import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import EntityNotFoundError, MarketMindCoreError
from app.domain.enums import AvailabilityStatus, IndicatorDirection
from app.repositories.indicator_repository import IndicatorRepository
from app.repositories.product_repository import ProductRepository
from app.services.indicator_engine import IndicatorEngine
from app.services.indicators.results import RawIndicatorResult

_ENGINE = "app.services.indicator_engine"
_AVAILABLE_WB = RawIndicatorResult(50.0, IndicatorDirection.DIRECT, "y_latest", AvailabilityStatus.AVAILABLE, None)
_AVAILABLE_INVERSE = RawIndicatorResult(2.0, IndicatorDirection.INVERSE, "last_3y", AvailabilityStatus.AVAILABLE, None)
_UNAVAILABLE = RawIndicatorResult(
    None, IndicatorDirection.DIRECT, "y_latest", AvailabilityStatus.UNAVAILABLE, "no data"
)
# Mirrors the real compute_worldbank_inflation_fallback contract: always FALLBACK by construction (see worldbank.py).
_FALLBACK_INVERSE = RawIndicatorResult(2.0, IndicatorDirection.INVERSE, "last_3y", AvailabilityStatus.FALLBACK, None)

_COUNTRY_LEVEL_TARGETS = [
    f"{_ENGINE}.worldbank_indicators.compute_gdp_per_capita",
    f"{_ENGINE}.worldbank_indicators.compute_internet_penetration",
    f"{_ENGINE}.worldbank_indicators.compute_mobile_connectivity",
    f"{_ENGINE}.worldbank_indicators.compute_gdp_growth",
    f"{_ENGINE}.worldbank_indicators.compute_population_scale",
    f"{_ENGINE}.worldbank_indicators.compute_urbanization",
    f"{_ENGINE}.undata_indicators.compute_population_growth",
    f"{_ENGINE}.undata_indicators.compute_net_migration_rate",
]

_PYTRENDS_TARGETS = {
    "trend": f"{_ENGINE}.pytrends_indicators.compute_trend_score",
    "growth": f"{_ENGINE}.pytrends_indicators.compute_growth_score",
    "stability": f"{_ENGINE}.pytrends_indicators.compute_demand_stability_score",
    "regional": f"{_ENGINE}.pytrends_indicators.compute_regional_demand_score",
    "intent": f"{_ENGINE}.pytrends_indicators.compute_search_intent_score",
}


def _unique_period_key() -> str:
    """Not a real 'YYYY-MM' period — fine here since every pytrends compute_*
    call is mocked in this file, so nothing ever parses it as a date. Unique
    per test so reruns against a persistent (not recreated) dev DB never
    collide with indicator_values/normalization_reference rows a previous
    run already committed for a fixed literal like "2026-07"."""
    return f"2026-07-test-{uuid.uuid4().hex[:8]}"


@contextmanager
def _patched_country_level(
    *, oecd_level: RawIndicatorResult, oecd_volatility: RawIndicatorResult | None = None
) -> Iterator[None]:
    with ExitStack() as stack:
        for target in _COUNTRY_LEVEL_TARGETS:
            stack.enter_context(patch(target, return_value=_AVAILABLE_WB))
        stack.enter_context(
            patch(
                f"{_ENGINE}.worldbank_indicators.compute_worldbank_inflation_fallback",
                return_value=_FALLBACK_INVERSE,
            )
        )
        stack.enter_context(patch(f"{_ENGINE}.oecd_indicators.compute_oecd_inflation_level", return_value=oecd_level))
        if oecd_volatility is not None:
            stack.enter_context(
                patch(f"{_ENGINE}.oecd_indicators.compute_oecd_inflation_volatility", return_value=oecd_volatility)
            )
        yield


@contextmanager
def _patched_pytrends(**overrides: RawIndicatorResult | list[RawIndicatorResult]) -> Iterator[None]:
    with ExitStack() as stack:
        for key, target in _PYTRENDS_TARGETS.items():
            value = overrides.get(key, _UNAVAILABLE)
            if isinstance(value, list):
                stack.enter_context(patch(target, side_effect=value))
            else:
                stack.enter_context(patch(target, return_value=value))
        stack.enter_context(patch(f"{_ENGINE}.undata_indicators.compute_median_age_fit", return_value=_UNAVAILABLE))
        yield


async def test_unknown_country_raises(db_session: AsyncSession) -> None:
    engine = IndicatorEngine(db_session)
    with pytest.raises(MarketMindCoreError):
        await engine.compute_all(country_iso3="ZZZ")


async def test_unknown_entity_raises(db_session: AsyncSession) -> None:
    engine = IndicatorEngine(db_session)
    with _patched_country_level(oecd_level=_UNAVAILABLE), pytest.raises(EntityNotFoundError):
        await engine.compute_all(country_iso3="USA", entity_key=uuid.uuid4(), period_key=_unique_period_key())


async def test_country_level_uses_worldbank_inflation_fallback_when_oecd_unavailable(db_session: AsyncSession) -> None:
    period_key = _unique_period_key()
    await IndicatorRepository(db_session).upsert_reference(
        indicator_code="gdp_per_capita_score", period_key=period_key, min_x=0, max_x=100,
        reference_population_size=27, is_closed=False,
    )  # fmt: skip
    await db_session.commit()

    engine = IndicatorEngine(db_session)
    with _patched_country_level(oecd_level=_UNAVAILABLE):
        results = await engine.compute_all(country_iso3="USA", period_key=period_key)

    by_code = {r.indicator_code: r for r in results}
    assert by_code["worldbank_inflation_score"].availability_status == "fallback"
    assert "oecd_inflation_level_score" not in by_code
    assert "oecd_inflation_volatility_score" not in by_code
    # gdp_per_capita_score has a reference row -> normalized; others don't -> None + note.
    assert by_code["gdp_per_capita_score"].normalized_score is not None
    assert by_code["mobile_connectivity_score"].normalized_score is None
    assert by_code["mobile_connectivity_score"].notes is not None
    assert all(r.entity_key is None for r in results)


async def test_oecd_available_skips_worldbank_inflation_fallback(db_session: AsyncSession) -> None:
    engine = IndicatorEngine(db_session)
    with _patched_country_level(oecd_level=_AVAILABLE_INVERSE, oecd_volatility=_AVAILABLE_INVERSE):
        results = await engine.compute_all(country_iso3="BRA", period_key=_unique_period_key())

    by_code = {r.indicator_code: r for r in results}
    assert "oecd_inflation_level_score" in by_code
    assert "oecd_inflation_volatility_score" in by_code
    assert "worldbank_inflation_score" not in by_code


async def test_product_specific_indicators_computed_with_entity_key(db_session: AsyncSession) -> None:
    entity = await ProductRepository(db_session).create_entity(canonical_title=f"Air Fryer {uuid.uuid4()}")
    await db_session.commit()

    trend = RawIndicatorResult(60.0, IndicatorDirection.DIRECT, "2026-07", AvailabilityStatus.AVAILABLE, None)

    engine = IndicatorEngine(db_session)
    with _patched_country_level(oecd_level=_UNAVAILABLE), _patched_pytrends(trend=trend):
        results = await engine.compute_all(
            country_iso3="USA", entity_key=entity.entity_key, period_key=_unique_period_key()
        )

    by_code = {r.indicator_code: r for r in results if r.entity_key is not None}
    assert by_code["trend_score"].raw_value == 60
    assert by_code["trend_score"].entity_key == entity.entity_key
    assert by_code["median_age_fit_score"].entity_key == entity.entity_key
    country_level = [r for r in results if r.entity_key is None]
    assert len(country_level) >= 8


async def test_granularity_fallback_marks_indicators_as_fallback(db_session: AsyncSession) -> None:
    entity = await ProductRepository(db_session).create_entity(
        canonical_title=f"Specific SKU {uuid.uuid4()}", category="air fryer"
    )
    await db_session.commit()

    category_trend = RawIndicatorResult(70.0, IndicatorDirection.DIRECT, "2026-07", AvailabilityStatus.AVAILABLE, None)
    category_growth = RawIndicatorResult(10.0, IndicatorDirection.DIRECT, "2026-07", AvailabilityStatus.AVAILABLE, None)

    engine = IndicatorEngine(db_session)
    with (
        _patched_country_level(oecd_level=_UNAVAILABLE),
        _patched_pytrends(trend=[_UNAVAILABLE, category_trend], growth=category_growth),
    ):
        results = await engine.compute_all(
            country_iso3="USA", entity_key=entity.entity_key, period_key=_unique_period_key()
        )

    by_code = {r.indicator_code: r for r in results if r.entity_key is not None}
    assert by_code["trend_score"].availability_status == "fallback"
    assert by_code["trend_score"].raw_value == 70
    assert "category" in (by_code["trend_score"].notes or "")
    assert by_code["growth_score"].availability_status == "fallback"
