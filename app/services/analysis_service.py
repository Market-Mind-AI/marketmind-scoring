"""Fase 5 — POST /analyze orchestration. Reuses CoverageService (Fase 1),
IndicatorEngine (Fase 3), and MosEngine (Fase 4) exactly as they are — this
module only sequences them, it never duplicates their logic. Runs inside a
FastAPI BackgroundTask (see app/api/routes/analyze.py) — v1, no dedicated
worker/queue yet (user decision 2026-07-08, see memory
marketmind_mos_engine): if the API process restarts mid-run, the
AnalysisRequest is left stuck in "running" with no resume.

"Últimos 6 meses" (user request) is a data-freshness window, not a request
to compute a MOS per month — the final answer is still the current month's
MOS per country, same contract as POST /mos/compute.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import MarketMindCoreError
from app.core.logging import logger
from app.db.models.country import Country
from app.db.models.product_catalog import ProductCatalog
from app.domain.enums import DataSource
from app.repositories.analysis_repository import AnalysisRepository
from app.repositories.country_repository import CountryRepository
from app.repositories.product_repository import ProductRepository
from app.schemas.coverage import CoverageGap
from app.schemas.mos import MosResultResponse
from app.services.coverage_service import WORLD_SENTINEL_ISO3, CoverageService
from app.services.indicator_engine import IndicatorEngine
from app.services.indicators import pytrends as pytrends_indicators
from app.services.indicators.oecd import RAW_INDICATOR_CODES as OECD_RAW_CODES
from app.services.indicators.undata import RAW_INDICATOR_CODES as UNDATA_RAW_CODES
from app.services.indicators.worldbank import RAW_INDICATOR_CODES as WORLDBANK_RAW_CODES
from app.services.mos_engine import MosEngine
from app.services.weight_calibration_service import WeightCalibrationService
from app.utils.period import current_month_period_key, last_n_month_period_keys
from app.utils.text import as_term_list, normalize_alias

_HISTORY_WINDOW_MONTHS = 6
# Window for collection_params on annual/quarterly sources (WorldBank/OECD/
# UNData) — generous on purpose so formulas needing several years of history
# (GDP Growth, Real Estate Growth, ...) have enough data; not tied precisely
# to the 6 monthly period_keys above, which only govern coverage_index
# freshness tracking, not how much history each source is asked to fetch.
_ANNUAL_LOOKBACK_YEARS = 10

# Per-country PyTrends collections (geo=XX): interest_over_time (Growth +
# Demand Stability), interest_by_region (Regional Demand — geographic spread
# inside the country), related_queries (Search Intent). trend_score is the
# WORLDWIDE interest_by_country (the cross-country "world total"), dispatched
# separately in _ensure_worldwide_demand_coverage.
_PYTRENDS_INDICATOR_CODES = ["interest_over_time", "interest_by_region", "related_queries"]
# English is always compared too (universal secondary search language) — see
# pytrends._read_languages / _UNIVERSAL_SEARCH_LANGUAGE.
_UNIVERSAL_SEARCH_LANGUAGE = "en"


class AnalysisService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._analysis_repo = AnalysisRepository(session)
        self._product_repo = ProductRepository(session)
        self._country_repo = CountryRepository(session)
        self._coverage_service = CoverageService(session)
        self._indicator_engine = IndicatorEngine(session)
        self._mos_engine = MosEngine(session)
        self._weight_calibration = WeightCalibrationService(session)

    async def run(self, request_id: uuid.UUID) -> None:
        analysis_request = await self._analysis_repo.get(request_id)
        if analysis_request is None:
            logger.warning(f"AnalysisRequest {request_id} not found — background task has nothing to do")
            return

        try:
            await self._analysis_repo.mark_running(analysis_request)
            await self._session.commit()

            entity_key = analysis_request.entity_key
            countries: list[str] = list(analysis_request.countries)

            if entity_key is not None:
                await self._ensure_worldwide_demand_coverage(entity_key=entity_key, countries=countries)
            for country_iso3 in countries:
                await self._ensure_coverage(country_iso3=country_iso3, entity_key=entity_key)

            period_key = current_month_period_key()
            for country_iso3 in countries:
                await self._indicator_engine.compute_all(
                    country_iso3=country_iso3, entity_key=entity_key, period_key=period_key
                )

            # Dimension weights are deterministic (Rank Reciprocal over a
            # fixed priority order — no country data involved), so calibrate
            # on demand here to guarantee they exist for this period. Without
            # this, a fresh database has no weights and compute_mos excludes
            # every country ("No calibrated weight for dimension=..."),
            # which is why the MOS previously needed a manual
            # POST /admin/mos/refresh-weights. Idempotent and respects the
            # is_closed immutability of already-locked periods.
            await self._weight_calibration.calibrate(period_key=period_key)

            # Completeness gate (user principle 2026-07-11): never compute a MOS
            # on partial data. Every requested country must carry the full,
            # uniform mandatory indicator set (same 0-100 ruler for all) before
            # any MOS is produced — otherwise the whole analysis fails loudly
            # rather than returning an asymmetric/partial ranking. Reliable
            # collection (retry + DataForSEO live fallback) is what makes this
            # gate pass; it is never satisfied by skipping a signal.
            if entity_key is not None:
                incomplete = await self._mos_engine.find_incomplete_countries(
                    entity_key=entity_key, countries=countries, period_key=period_key
                )
                if incomplete:
                    detail = "; ".join(f"{country}: {reason}" for country, reason in incomplete.items())
                    raise MarketMindCoreError(
                        "Analysis aborted — the MOS is never computed on partial data. "
                        f"Incomplete after collection: {detail}"
                    )

            mos_results = (
                await self._mos_engine.compute_mos(entity_key=entity_key, countries=countries, period_key=period_key)
                if entity_key is not None
                else []
            )
            serialized = [
                MosResultResponse.model_validate(r, from_attributes=True).model_dump(mode="json") for r in mos_results
            ]
            await self._analysis_repo.mark_done(analysis_request, result=serialized)
            await self._session.commit()
        except Exception as exc:  # noqa: BLE001 — background task boundary, must never crash the process
            logger.exception(f"AnalysisRequest {request_id} failed")
            await self._session.rollback()
            failed_request = await self._analysis_repo.get(request_id)
            if failed_request is not None:
                await self._analysis_repo.mark_failed(failed_request, error_message=str(exc))
                await self._session.commit()

    async def _ensure_coverage(self, *, country_iso3: str, entity_key: uuid.UUID | None) -> None:
        """Checks (read-only) the last 6 monthly period_keys for every gap
        this country/product needs, and only dispatches collection
        (check_or_collect) for the ones actually missing."""
        period_keys = last_n_month_period_keys(_HISTORY_WINDOW_MONTHS)
        gaps: list[CoverageGap] = []

        if entity_key is not None:
            entity = await self._product_repo.get_by_entity_key(entity_key)
            country = await self._country_repo.get_by_iso3(country_iso3)
            # Per-country demand is searched in the country's own language
            # (2026-07-10): a Portuguese buyer searches "presunto", not the
            # Spanish "jamón", so measuring Portugal with the Spanish term
            # understates it. Falls back to canonical_title when the country
            # has no search_language or the entity has no term for it.
            # Per-country series are collected with the country's WINNING local
            # term (the variation that scored highest in the worldwide
            # interest_by_country arbitration — real volume), falling back to
            # the LLM's first guess. Matches what indicator_engine reads. The
            # worldwide collection ran first (above), so the verdict exists.
            keyword = None
            if entity is not None:
                keyword = _localized_keyword(entity, country) or entity.canonical_title
                country_code = (country.pytrends_geo or country.iso2) if country is not None else None
                if country is not None and country_code is not None:
                    keyword = await pytrends_indicators.best_localized_term(
                        entity.canonical_title, country_code, country.search_language, keyword
                    )
            if keyword is not None:
                # Per-country PyTrends is collected ONCE (current month only),
                # not per freshness period_key: each call fetches the same
                # rolling "today 12-m" series regardless of period_key, so the
                # old 6x loop just re-fetched identical data 6x — 6x the
                # rate-limited Google calls for nothing. One gap per indicator;
                # next month's period_key re-collects for freshness. (Same
                # rationale as the worldwide interest_by_country gap.)
                for indicator_code in _PYTRENDS_INDICATOR_CODES:
                    gaps.append(
                        CoverageGap(
                            source=DataSource.PYTRENDS,
                            entity_key=entity_key,
                            country=country_iso3,
                            indicator_code=indicator_code,
                            period_key=current_month_period_key(),
                            collection_params={"keyword": keyword},
                        )
                    )

        current_year = datetime.now(UTC).year
        start_year, end_year = current_year - _ANNUAL_LOOKBACK_YEARS, current_year

        for period_key in period_keys:
            for codes in WORLDBANK_RAW_CODES.values():
                for code in codes:
                    gaps.append(
                        CoverageGap(
                            source=DataSource.WORLDBANK,
                            entity_key=None,
                            country=country_iso3,
                            indicator_code=code,
                            period_key=period_key,
                            collection_params={"start_year": start_year, "end_year": end_year},
                        )
                    )
            for codes in OECD_RAW_CODES.values():
                for code in codes:
                    gaps.append(
                        CoverageGap(
                            source=DataSource.OECD,
                            entity_key=None,
                            country=country_iso3,
                            indicator_code=code,
                            period_key=period_key,
                            collection_params={"start_period": str(start_year), "end_period": str(end_year)},
                        )
                    )
            for codes in UNDATA_RAW_CODES.values():
                for code in codes:
                    gaps.append(
                        CoverageGap(
                            source=DataSource.UNDATA,
                            entity_key=None,
                            country=country_iso3,
                            indicator_code=code,
                            period_key=period_key,
                            collection_params={"start_year": start_year, "end_year": end_year},
                        )
                    )

        if not gaps:
            return

        # Dedup — some sources repeat the same native code across dimensions
        # (e.g. OECD's "CPI" backs both inflation level and volatility).
        seen: set[tuple[str, str, str, str]] = set()
        unique_gaps: list[CoverageGap] = []
        for gap in gaps:
            key = (gap.source.value, gap.indicator_code, gap.country, gap.period_key)
            if key in seen:
                continue
            seen.add(key)
            unique_gaps.append(gap)

        statuses = await self._coverage_service.check_coverage(unique_gaps)
        missing = [gap for gap, status in zip(unique_gaps, statuses, strict=True) if not status.covered]
        if missing:
            await self._coverage_service.check_or_collect(missing, requested_by="analysis_service")

    async def _ensure_worldwide_demand_coverage(self, *, entity_key: uuid.UUID, countries: list[str]) -> None:
        """The cross-country-comparable demand signal (interest_by_country)
        is a WORLDWIDE Google Trends query — one collection per product, not
        per country. Dispatched once here under the sentinel ISO3 "WLD",
        separate from the per-country coverage loop, so it isn't re-fetched
        once per requested country. Feeds compute_trend_score (see
        app/services/indicators/pytrends.py).

        Multi-term (2026-07-10): sends one localized term per distinct search
        language among the requested countries, all compared on a single
        Google Trends axis so each country can be read against the term in
        ITS OWN language (keyword localization). base_keyword stays the
        canonical title (grouping key / the row's `keyword`)."""
        entity = await self._product_repo.get_by_entity_key(entity_key)
        if entity is None:
            return
        base_keyword = entity.canonical_title

        # Terms compared on one Google Trends axis: each requested country's
        # own-language variations PLUS the English variations (the universal
        # secondary — many markets search global products in English; see
        # pytrends._read_languages). Real search volume then arbitrates which
        # name is the genuine one per country, and compute_trend_score reads
        # MAX over (country language, English).
        #
        # Deduped by normalized TERM STRING (not language+string): a loanword
        # like "airfryer" that appears in several language lists must be sent
        # ONCE — sending it multiple times put the same string in different
        # anchor-chained batches and produced inconsistent values for it
        # (bug found 2026-07-14: DEU read "airfryer"=40 while another tag had
        # 79). First language seen for a string wins the tag; the MAX read
        # still picks it up for any country via the English inclusion.
        languages: list[str] = []
        for country_iso3 in countries:
            country = await self._country_repo.get_by_iso3(country_iso3)
            language = country.search_language if country is not None else None
            if language is not None and language not in languages:
                languages.append(language)
        if _UNIVERSAL_SEARCH_LANGUAGE not in languages:
            languages.append(_UNIVERSAL_SEARCH_LANGUAGE)

        terms: list[dict[str, str]] = []
        seen_strings: set[str] = set()
        for language in languages:
            for term in _language_variations(entity, language) or [base_keyword]:
                norm = normalize_alias(term)
                if norm in seen_strings:
                    continue
                seen_strings.add(norm)
                terms.append({"term": term, "language": language})
        if not terms:
            terms = [{"term": base_keyword, "language": "und"}]  # no language mapping at all

        # A single collection, keyed only to the current month. Unlike the
        # per-country signals, the worldwide breakdown is one snapshot (not a
        # monthly series), so collecting it once per freshness period would
        # just re-fetch identical data 6x (6 rate-limited calls). One gap =
        # one call; next month's period_key re-collects for freshness.
        # The cross-country-comparable "world total": worldwide multi-term
        # interest_by_country (geo='', one localized term per language on a
        # single global axis) — feeds trend_score. Keyed to the current month
        # only (single snapshot, not a monthly series).
        gap = CoverageGap(
            source=DataSource.PYTRENDS,
            entity_key=entity_key,
            country=WORLD_SENTINEL_ISO3,
            indicator_code="interest_by_country",
            period_key=current_month_period_key(),
            collection_params={"base_keyword": base_keyword, "terms": terms},
        )
        statuses = await self._coverage_service.check_coverage([gap])
        if not statuses[0].covered:
            await self._coverage_service.check_or_collect([gap], requested_by="analysis_service")


def _language_variations(entity: "ProductCatalog", language: str | None) -> list[str]:
    """All accumulated local-name variations for a language, tolerating the
    legacy single-string shape. Empty if the language isn't in the acervo."""
    if language is None:
        return []
    raw = (entity.localized_terms or {}).get(language)
    return as_term_list(raw)


def _localized_keyword(entity: "ProductCatalog | None", country: "Country | None") -> str | None:
    """The representative per-country search term = the top (best-guess-first)
    local-name variation in that country's search language, falling back to the
    canonical title. Used for the per-country geo signals (growth/stability/
    regional), which take a single keyword; the worldwide demand read SUMS all
    variations instead (see compute_trend_score)."""
    if entity is None:
        return None
    language = country.search_language if country is not None else None
    variations = _language_variations(entity, language)
    return variations[0] if variations else entity.canonical_title
