"""Fase 3: computes/refreshes normalization_reference bounds (Cap. 4 §4.1
correction — percentile p5/p95 against the full reference population, not
per-query min/max; see docs/marketmind-implementation-plan.md section 7).

Reference population = the seeded `countries` table (~27 countries) —
"cresce organicamente" via user queries isn't tracked yet, a documented
simplification (see memory marketmind_mos_engine, item "População de
referência"). Respects is_closed immutability via
IndicatorRepository.upsert_reference — never overwrites a closed period.

Only the 9 empirical country-agnostic indicators need this (Growth Score
and Demand Stability use fixed theoretical bounds seeded once by migration
005, not recomputed here — see app/services/indicators/normalization.py).
"""

import math

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger
from app.db.models.country import Country
from app.domain.enums import DataSource, IndicatorCode
from app.repositories.indicator_repository import IndicatorRepository
from app.services.collection_client import resolve_source_country_code
from app.services.indicators import oecd as oecd_indicators
from app.services.indicators import undata as undata_indicators
from app.services.indicators import worldbank as worldbank_indicators
from app.services.indicators.results import RawIndicatorResult
from app.utils.period import current_month_period_key

_LOWER_PERCENTILE = 5
_UPPER_PERCENTILE = 95

_EMPIRICAL_CODES: list[IndicatorCode] = [
    IndicatorCode.GDP_PER_CAPITA_SCORE,
    IndicatorCode.MOBILE_CONNECTIVITY_SCORE,
    IndicatorCode.GDP_GROWTH_SCORE,
    IndicatorCode.POPULATION_SCALE_SCORE,
    IndicatorCode.POPULATION_GROWTH_SCORE,
    IndicatorCode.NET_MIGRATION_RATE_SCORE,
    IndicatorCode.WORLDBANK_INFLATION_SCORE,
    IndicatorCode.OECD_INFLATION_LEVEL_SCORE,
    IndicatorCode.OECD_INFLATION_VOLATILITY_SCORE,
]


def _percentile(values: list[float], pct: float) -> float:
    """Linear interpolation between closest ranks (numpy's default
    'linear' method) — no numpy dependency for a handful of countries."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100) * (len(ordered) - 1)
    lower_index, upper_index = math.floor(rank), math.ceil(rank)
    if lower_index == upper_index:
        return ordered[lower_index]
    fraction = rank - lower_index
    return ordered[lower_index] + (ordered[upper_index] - ordered[lower_index]) * fraction


async def _compute_single(indicator_code: IndicatorCode, country: Country, iso2: str) -> RawIndicatorResult:
    if indicator_code == IndicatorCode.GDP_PER_CAPITA_SCORE:
        return await worldbank_indicators.compute_gdp_per_capita(iso2)
    if indicator_code == IndicatorCode.MOBILE_CONNECTIVITY_SCORE:
        return await worldbank_indicators.compute_mobile_connectivity(iso2)
    if indicator_code == IndicatorCode.GDP_GROWTH_SCORE:
        return await worldbank_indicators.compute_gdp_growth(iso2)
    if indicator_code == IndicatorCode.POPULATION_SCALE_SCORE:
        return await worldbank_indicators.compute_population_scale(iso2)
    if indicator_code == IndicatorCode.POPULATION_GROWTH_SCORE:
        return await undata_indicators.compute_population_growth(iso2)
    if indicator_code == IndicatorCode.NET_MIGRATION_RATE_SCORE:
        return await undata_indicators.compute_net_migration_rate(iso2)
    if indicator_code == IndicatorCode.WORLDBANK_INFLATION_SCORE:
        return await worldbank_indicators.compute_worldbank_inflation_fallback(iso2)
    if indicator_code == IndicatorCode.OECD_INFLATION_LEVEL_SCORE:
        return await oecd_indicators.compute_oecd_inflation_level(resolve_source_country_code(DataSource.OECD, country))
    if indicator_code == IndicatorCode.OECD_INFLATION_VOLATILITY_SCORE:
        return await oecd_indicators.compute_oecd_inflation_volatility(
            resolve_source_country_code(DataSource.OECD, country)
        )
    raise ValueError(f"{indicator_code} is not an empirical-reference indicator")


class ReferenceCalculatorService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._indicator_repo = IndicatorRepository(session)

    async def refresh(self, *, period_key: str | None = None) -> dict[str, int]:
        """Recomputes normalization_reference for every empirical indicator
        against the current reference population. Returns
        {indicator_code: reference_population_size} — 0 means the
        population was too small (< 2 valid values) and that indicator was
        skipped, not silently zeroed."""
        resolved_period_key = period_key or current_month_period_key()
        countries_result = await self._session.execute(select(Country))
        countries = list(countries_result.scalars().all())

        report: dict[str, int] = {}
        for code in _EMPIRICAL_CODES:
            values = await self._collect_raw_values(code, countries)
            if len(values) < 2:
                logger.warning(f"Reference population too small for {code.value}: {len(values)} value(s), skipping")
                report[code.value] = 0
                continue

            min_x = _percentile(values, _LOWER_PERCENTILE)
            max_x = _percentile(values, _UPPER_PERCENTILE)
            if max_x <= min_x:
                # Degenerate distribution (reference countries all near the
                # same value) — normalize() also guards max(X)=min(X), but
                # keep the stored bounds non-degenerate regardless.
                max_x = min_x + 1

            await self._indicator_repo.upsert_reference(
                indicator_code=code.value,
                period_key=resolved_period_key,
                min_x=min_x,
                max_x=max_x,
                reference_population_size=len(values),
                is_closed=False,
            )
            report[code.value] = len(values)

        await self._session.commit()
        return report

    async def _collect_raw_values(self, indicator_code: IndicatorCode, countries: list[Country]) -> list[float]:
        values: list[float] = []
        for country in countries:
            if country.iso2 is None:
                continue
            result = await _compute_single(indicator_code, country, country.iso2)
            if result.raw_value is not None:
                values.append(float(result.raw_value))
        return values
