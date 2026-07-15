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
_CONDITIONAL_UNAVAILABLE = RawIndicatorResult(
    None, IndicatorDirection.DIRECT, "last_12q", AvailabilityStatus.CONDITIONAL, "insufficient RHP quarters"
)

_COUNTRY_LEVEL_TARGETS = [
    f"{_ENGINE}.worldbank_indicators.compute_gdp_per_capita",
    f"{_ENGINE}.worldbank_indicators.compute_internet_penetration",
    f"{_ENGINE}.worldbank_indicators.compute_mobile_connectivity",
    f"{_ENGINE}.worldbank_indicators.compute_gdp_growth",
    f"{_ENGINE}.worldbank_indicators.compute_population_scale",
    f"{_ENGINE}.worldbank_indicators.compute_urbanization",
    f"{_ENGINE}.undata_indicators.compute_population_growth",
    f"{_ENGINE}.undata_indicators.compute_net_migration_rate",
    # Macro Demand Structure + Real Estate Growth (added 2026-07-08)
    f"{_ENGINE}.worldbank_indicators.compute_consumer_spending",
    f"{_ENGINE}.worldbank_indicators.compute_government_expenditure",
    f"{_ENGINE}.worldbank_indicators.compute_investment",
    f"{_ENGINE}.worldbank_indicators.compute_net_exports",
    f"{_ENGINE}.worldbank_indicators.compute_trade_openness",
]

# Demand from Google Ads absolute search volume (2026-07-15): 4 indicators,
# no regional_demand and no granularity fallback.
_PYTRENDS_TARGETS = {
    "trend": f"{_ENGINE}.pytrends_indicators.compute_trend_score",
    "growth": f"{_ENGINE}.pytrends_indicators.compute_growth_score",
    "stability": f"{_ENGINE}.pytrends_indicators.compute_demand_stability_score",
    "intent": f"{_ENGINE}.pytrends_indicators.compute_search_intent_score",
}


def _unique_period_key() -> str:
    """Not a real 'YYYY-MM' period — fine here since every pytrends compute_*
    call is mocked in this file, so nothing ever parses it as a date. Unique
    per test so reruns against a persistent (not recreated) dev DB never
    collide with indicator_values rows a previous run already committed for a
    fixed literal like "2026-07". (Fixed normalization bounds are keyed on
    period_key="FIXED", independent of this.)"""
    return f"2026-07-test-{uuid.uuid4().hex[:8]}"


@contextmanager
def _patched_country_level(*, inflation: RawIndicatorResult = _AVAILABLE_INVERSE) -> Iterator[None]:
    with ExitStack() as stack:
        for target in _COUNTRY_LEVEL_TARGETS:
            stack.enter_context(patch(target, return_value=_AVAILABLE_WB))
        stack.enter_context(patch(f"{_ENGINE}.worldbank_indicators.compute_inflation", return_value=inflation))
        stack.enter_context(
            patch(f"{_ENGINE}.oecd_indicators.compute_real_estate_growth", return_value=_CONDITIONAL_UNAVAILABLE)
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
    with _patched_country_level(), pytest.raises(EntityNotFoundError):
        await engine.compute_all(country_iso3="USA", entity_key=uuid.uuid4(), period_key=_unique_period_key())


async def test_country_level_inflation_from_worldbank_only(db_session: AsyncSession) -> None:
    # World Bank is the sole inflation source now (OECD path removed
    # 2026-07-08); it's AVAILABLE like any other WB indicator, no OECD
    # inflation codes ever appear in the output.
    period_key = _unique_period_key()
    # gdp_per_capita_score's fixed bound is seeded by migration 010 under
    # period_key="FIXED"; seed it here too since db_session may run against a
    # DB where that migration ran, but keep the test self-contained.
    await IndicatorRepository(db_session).upsert_reference(
        indicator_code="gdp_per_capita_score", period_key="FIXED", min_x=500, max_x=100000,
        reference_population_size=0, is_closed=False,
    )  # fmt: skip
    await db_session.commit()

    engine = IndicatorEngine(db_session)
    with _patched_country_level():
        results = await engine.compute_all(country_iso3="USA", period_key=period_key)

    by_code = {r.indicator_code: r for r in results}
    assert by_code["worldbank_inflation_score"].availability_status == "available"
    assert "oecd_inflation_level_score" not in by_code
    assert "oecd_inflation_volatility_score" not in by_code
    # gdp_per_capita_score has a FIXED reference row -> normalized.
    assert by_code["gdp_per_capita_score"].normalized_score is not None
    assert all(r.entity_key is None for r in results)


async def test_product_specific_indicators_computed_with_entity_key(db_session: AsyncSession) -> None:
    # Demand is now read per country from the product's approved localized terms
    # (product_catalog.localized_markets[ISO3]); the engine saves 4 pytrends
    # indicators (trend/growth/stability/intent — no regional_demand) plus the
    # conditional median-age fit.
    entity = await ProductRepository(db_session).create_entity(
        canonical_title=f"Air Fryer {uuid.uuid4()}",
        localized_markets={"USA": {"primary_name": "air fryer", "search_terms": ["air fryer", "airfryer"]}},
    )
    await db_session.commit()

    trend = RawIndicatorResult(60.0, IndicatorDirection.DIRECT, "2026-07", AvailabilityStatus.AVAILABLE, None)

    engine = IndicatorEngine(db_session)
    with _patched_country_level(), _patched_pytrends(trend=trend):
        results = await engine.compute_all(
            country_iso3="USA", entity_key=entity.entity_key, period_key=_unique_period_key()
        )

    by_code = {r.indicator_code: r for r in results if r.entity_key is not None}
    assert by_code["trend_score"].raw_value == 60
    assert by_code["trend_score"].entity_key == entity.entity_key
    # 4 pytrends Demand indicators + median-age fit, all keyed to the entity.
    assert {"trend_score", "growth_score", "demand_stability_score", "search_intent_score"} <= set(by_code)
    assert "regional_demand_score" not in by_code
    assert by_code["median_age_fit_score"].entity_key == entity.entity_key
    country_level = [r for r in results if r.entity_key is None]
    assert len(country_level) >= 8
