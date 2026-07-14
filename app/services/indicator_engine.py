"""Fase 3: Cap. 4 orchestration. For a given (country, optional entity,
period), computes every applicable indicator's raw value, normalizes it,
and upserts into indicator_values. Never computes MOS (Fase 4) — this is
purely the indicator layer, one function per Cap. 4 indicator.

Country-level indicators (GDP, Internet, Mobile, GDP Growth, Population,
Urbanization, Inflation, Population Growth, Net Migration — Cap. 4 "país-a-
país product-agnostic") are always computed with entity_key=NULL and are
shared across every product for that country+period. Product-specific
indicators (Trend/Growth/Demand Stability/Regional Demand/Search Intent,
plus the conditional Median Age Fit) only run when an entity_key is given.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import EntityNotFoundError, MarketMindCoreError
from app.core.logging import logger
from app.db.models.country import Country
from app.db.models.indicator_value import IndicatorValue
from app.db.models.product_catalog import ProductCatalog
from app.domain.enums import (
    AvailabilityStatus,
    DataSource,
    IndicatorCode,
    IndicatorDirection,
    NormalizationMethod,
    PeriodType,
)
from app.repositories.country_repository import CountryRepository
from app.repositories.indicator_repository import IndicatorRepository
from app.repositories.product_repository import ProductRepository
from app.services.collection_client import resolve_source_country_code
from app.services.indicators import oecd as oecd_indicators
from app.services.indicators import pytrends as pytrends_indicators
from app.services.indicators import undata as undata_indicators
from app.services.indicators import worldbank as worldbank_indicators
from app.services.indicators.normalization import FIXED_BOUNDS_PERIOD_KEY, normalization_method, normalize
from app.services.indicators.results import RawIndicatorResult
from app.services.mos.interpretation import INDICATOR_MEANING, score_band
from app.utils.period import current_month_period_key
from app.utils.text import as_term_list


class IndicatorEngine:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._indicator_repo = IndicatorRepository(session)
        self._country_repo = CountryRepository(session)
        self._product_repo = ProductRepository(session)

    async def compute_all(
        self, *, country_iso3: str, entity_key: uuid.UUID | None = None, period_key: str | None = None
    ) -> list[IndicatorValue]:
        resolved_period_key = period_key or current_month_period_key()
        country = await self._country_repo.get_by_iso3(country_iso3)
        if country is None:
            raise MarketMindCoreError(f"Unknown country {country_iso3} (not in canonical countries table)")

        results = await self._compute_country_level_indicators(country, resolved_period_key)

        if entity_key is not None:
            entity = await self._product_repo.get_by_entity_key(entity_key)
            if entity is None:
                raise EntityNotFoundError(f"Entity {entity_key} not found")
            results += await self._compute_pytrends_indicators(entity, country, resolved_period_key)
            results.append(await self._compute_median_age_fit(entity, country, resolved_period_key))

        await self._session.commit()
        return results

    async def _compute_country_level_indicators(self, country: Country, period_key: str) -> list[IndicatorValue]:
        if country.iso2 is None:
            logger.warning(f"Country {country.iso3} has no ISO2 mapping — country-level indicators skipped")
            missing = RawIndicatorResult(
                None, IndicatorDirection.DIRECT, "n/a", AvailabilityStatus.UNAVAILABLE,
                f"country {country.iso3} has no iso2 mapping",
            )  # fmt: skip
            codes = [
                IndicatorCode.GDP_PER_CAPITA_SCORE, IndicatorCode.INTERNET_PENETRATION_SCORE,
                IndicatorCode.MOBILE_CONNECTIVITY_SCORE, IndicatorCode.GDP_GROWTH_SCORE,
                IndicatorCode.POPULATION_SCALE_SCORE, IndicatorCode.URBANIZATION_SCORE,
                IndicatorCode.POPULATION_GROWTH_SCORE, IndicatorCode.NET_MIGRATION_RATE_SCORE,
                IndicatorCode.WORLDBANK_INFLATION_SCORE, IndicatorCode.CONSUMER_SPENDING_SCORE,
                IndicatorCode.INVESTMENT_SCORE, IndicatorCode.NET_EXPORTS_SCORE,
                IndicatorCode.TRADE_OPENNESS_SCORE, IndicatorCode.GOVERNMENT_EXPENDITURE_SCORE,
                IndicatorCode.REAL_ESTATE_GROWTH_SCORE,
            ]  # fmt: skip
            return [
                await self._normalize_and_save(code, DataSource.WORLDBANK, None, country.iso3, period_key, missing)
                for code in codes
            ]

        wb_iso2 = country.iso2
        un_iso2 = country.iso2
        oecd_code = resolve_source_country_code(DataSource.OECD, country)

        gdp_per_capita = await worldbank_indicators.compute_gdp_per_capita(wb_iso2)
        internet = await worldbank_indicators.compute_internet_penetration(wb_iso2)
        mobile = await worldbank_indicators.compute_mobile_connectivity(wb_iso2)
        gdp_growth = await worldbank_indicators.compute_gdp_growth(wb_iso2)
        population = await worldbank_indicators.compute_population_scale(wb_iso2)
        urbanization = await worldbank_indicators.compute_urbanization(wb_iso2)
        population_growth = await undata_indicators.compute_population_growth(un_iso2)
        net_migration = await undata_indicators.compute_net_migration_rate(un_iso2)

        # Macro Demand Structure (2026-07-08, GDP = C+I+G+NX identity) +
        # Real Estate Growth (Economic Readiness, conditional/OECD-only).
        consumer_spending = await worldbank_indicators.compute_consumer_spending(wb_iso2)
        government_expenditure = await worldbank_indicators.compute_government_expenditure(wb_iso2)
        investment = await worldbank_indicators.compute_investment(wb_iso2)
        net_exports = await worldbank_indicators.compute_net_exports(wb_iso2)
        trade_openness = await worldbank_indicators.compute_trade_openness(wb_iso2)
        real_estate_growth = await oecd_indicators.compute_real_estate_growth(oecd_code)

        raws: list[tuple[IndicatorCode, DataSource, RawIndicatorResult]] = [
            (IndicatorCode.GDP_PER_CAPITA_SCORE, DataSource.WORLDBANK, gdp_per_capita),
            (IndicatorCode.INTERNET_PENETRATION_SCORE, DataSource.WORLDBANK, internet),
            (IndicatorCode.MOBILE_CONNECTIVITY_SCORE, DataSource.WORLDBANK, mobile),
            (IndicatorCode.GDP_GROWTH_SCORE, DataSource.WORLDBANK, gdp_growth),
            (IndicatorCode.POPULATION_SCALE_SCORE, DataSource.WORLDBANK, population),
            (IndicatorCode.URBANIZATION_SCORE, DataSource.WORLDBANK, urbanization),
            (IndicatorCode.POPULATION_GROWTH_SCORE, DataSource.UNDATA, population_growth),
            (IndicatorCode.NET_MIGRATION_RATE_SCORE, DataSource.UNDATA, net_migration),
            (IndicatorCode.CONSUMER_SPENDING_SCORE, DataSource.WORLDBANK, consumer_spending),
            (IndicatorCode.GOVERNMENT_EXPENDITURE_SCORE, DataSource.WORLDBANK, government_expenditure),
            (IndicatorCode.INVESTMENT_SCORE, DataSource.WORLDBANK, investment),
            (IndicatorCode.NET_EXPORTS_SCORE, DataSource.WORLDBANK, net_exports),
            (IndicatorCode.TRADE_OPENNESS_SCORE, DataSource.WORLDBANK, trade_openness),
            (IndicatorCode.REAL_ESTATE_GROWTH_SCORE, DataSource.OECD, real_estate_growth),
        ]

        # Inflation: World Bank only (2026-07-08) — OECD's CPI series was
        # removed as a source. marketmind-oecd doesn't persist the
        # EXPENDITURE/TRANSFORMATION/ADJUSTMENT dimensions Cap. 4 needs to
        # isolate a clean annual-growth series, so `measure='CPI'` alone
        # was averaging several unrelated rebased sub-indices together
        # (confirmed live: same time_period, wildly different values —
        # e.g. Spain "124% inflation" was actually a mix of index levels,
        # not a real annual rate). See marketmind_mos_engine.
        inflation = await worldbank_indicators.compute_inflation(wb_iso2)
        raws.append((IndicatorCode.WORLDBANK_INFLATION_SCORE, DataSource.WORLDBANK, inflation))

        return [
            await self._normalize_and_save(code, source, None, country.iso3, period_key, raw)
            for code, source, raw in raws
        ]

    async def _compute_pytrends_indicators(
        self, entity: ProductCatalog, country: Country, period_key: str
    ) -> list[IndicatorValue]:
        country_code = resolve_source_country_code(DataSource.PYTRENDS, country)
        # trend_score reads the WORLDWIDE interest_by_country (the cross-country
        # comparable "world total") for (canonical base keyword, this country's
        # language). growth/stability/regional read the PER-COUNTRY series
        # (interest_over_time, interest_by_region, geo=XX) searched with the
        # country's WINNING local term — the variation that scored highest in
        # the worldwide arbitration (has real volume), not the LLM's first
        # guess (which can be too niche to have a standalone series). Matches
        # the term analysis_service collected for those per-country series.
        language = country.search_language
        variations = as_term_list((entity.localized_terms or {}).get(language)) if language is not None else []
        fallback_keyword = variations[0] if variations else entity.canonical_title
        keyword = await pytrends_indicators.best_localized_term(
            entity.canonical_title, country_code, language, fallback_keyword
        )

        trend = await pytrends_indicators.compute_trend_score(
            entity.canonical_title, country_code, period_key, term_language=language
        )
        granularity_fallback = False
        if trend.availability_status == AvailabilityStatus.UNAVAILABLE and entity.category:
            # Cap. 4 "fallback de granularidade SKU->categoria": specific
            # keyword has insufficient volume, retry with the category term.
            fallback_trend = await pytrends_indicators.compute_trend_score(
                entity.category, country_code, period_key, term_language=language
            )
            if fallback_trend.availability_status == AvailabilityStatus.AVAILABLE:
                keyword = entity.category
                granularity_fallback = True
                trend = fallback_trend

        growth = await pytrends_indicators.compute_growth_score(keyword, country_code, period_key)
        stability = await pytrends_indicators.compute_demand_stability_score(keyword, country_code, period_key)
        regional = await pytrends_indicators.compute_regional_demand_score(keyword, country_code, period_key)
        intent = await pytrends_indicators.compute_search_intent_score(keyword, period_key, language=language)

        indicator_raws = [
            (IndicatorCode.TREND_SCORE, trend),
            (IndicatorCode.GROWTH_SCORE, growth),
            (IndicatorCode.DEMAND_STABILITY_SCORE, stability),
            (IndicatorCode.REGIONAL_DEMAND_SCORE, regional),
            (IndicatorCode.SEARCH_INTENT_SCORE, intent),
        ]

        saved: list[IndicatorValue] = []
        for code, raw in indicator_raws:
            if granularity_fallback and raw.availability_status == AvailabilityStatus.AVAILABLE:
                raw = RawIndicatorResult(
                    raw.raw_value, raw.direction, raw.calculation_window, AvailabilityStatus.FALLBACK,
                    f"granularity fallback: keyword='{keyword}' (category) instead of canonical_title "
                    f"'{entity.canonical_title}' (Cap. 4 fallback de granularidade SKU->categoria)",
                )  # fmt: skip
            saved.append(
                await self._normalize_and_save(
                    code, DataSource.PYTRENDS, entity.entity_key, country.iso3, period_key, raw
                )
            )
        return saved

    async def _compute_median_age_fit(
        self, entity: ProductCatalog, country: Country, period_key: str
    ) -> IndicatorValue:
        un_iso2 = country.iso2
        if un_iso2 is None:
            raw = RawIndicatorResult(
                None, IndicatorDirection.DIRECT, "n/a", AvailabilityStatus.UNAVAILABLE,
                f"country {country.iso3} has no iso2 mapping",
            )  # fmt: skip
        else:
            raw = await undata_indicators.compute_median_age_fit(un_iso2, entity.target_age_min, entity.target_age_max)
        return await self._normalize_and_save(
            IndicatorCode.MEDIAN_AGE_FIT_SCORE, DataSource.UNDATA, entity.entity_key, country.iso3, period_key, raw
        )

    async def _normalize_and_save(
        self,
        indicator_code: IndicatorCode,
        source: DataSource,
        entity_key: uuid.UUID | None,
        country_iso3: str,
        period_key: str,
        raw: RawIndicatorResult,
    ) -> IndicatorValue:
        normalized_score: float | None = None
        note = raw.notes

        if raw.raw_value is not None:
            method = normalization_method(indicator_code)
            reference = None
            if method == NormalizationMethod.MINMAX_FIXED:
                # Self-widening (never narrows) — see
                # indicator_repository.py::widen_reference_bounds. Never
                # compares this country against any other country in the
                # same request, only against its own indicator's fixed,
                # absolute yardstick.
                reference = await self._indicator_repo.widen_reference_bounds(
                    indicator_code.value, FIXED_BOUNDS_PERIOD_KEY, float(raw.raw_value)
                )

            normalized_score, normalize_note = normalize(
                indicator_code=indicator_code, raw_value=raw.raw_value, direction=raw.direction, reference=reference
            )
            if normalize_note is not None:
                note = f"{note}; {normalize_note}" if note else normalize_note

        return await self._indicator_repo.upsert_value(
            entity_key=entity_key,
            country=country_iso3,
            indicator_code=indicator_code.value,
            period_type=PeriodType.MONTH.value,
            period_key=period_key,
            source=source.value,
            raw_value=raw.raw_value,
            normalized_score=normalized_score,
            direction=raw.direction.value,
            calculation_window=raw.calculation_window,
            availability_status=raw.availability_status.value,
            notes=note,
            business_context=INDICATOR_MEANING.get(indicator_code),
            interpretation_band=None if normalized_score is None else score_band(normalized_score),
        )
