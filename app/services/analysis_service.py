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
from app.domain.enums import DataSource
from app.repositories.analysis_repository import AnalysisRepository
from app.repositories.country_repository import CountryRepository
from app.repositories.product_repository import ProductRepository
from app.schemas.coverage import CoverageGap
from app.schemas.mos import MosResultResponse
from app.services.coverage_service import CoverageService
from app.services.indicator_engine import IndicatorEngine
from app.services.indicators.oecd import RAW_INDICATOR_CODES as OECD_RAW_CODES
from app.services.indicators.undata import RAW_INDICATOR_CODES as UNDATA_RAW_CODES
from app.services.indicators.worldbank import RAW_INDICATOR_CODES as WORLDBANK_RAW_CODES
from app.services.mos_engine import MosEngine
from app.services.weight_calibration_service import WeightCalibrationService
from app.utils.period import current_month_period_key, last_n_month_period_keys
from app.utils.text import as_term_list

_HISTORY_WINDOW_MONTHS = 6
# Window for collection_params on annual/quarterly sources (WorldBank/OECD/
# UNData) — generous on purpose so formulas needing several years of history
# (GDP Growth, Real Estate Growth, ...) have enough data; not tied precisely
# to the 6 monthly period_keys above, which only govern coverage_index
# freshness tracking, not how much history each source is asked to fetch.
_ANNUAL_LOOKBACK_YEARS = 10


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
            # Demand is collected from Google Ads keyword data (2026-07-15) with
            # the country's OWN user-approved terms (localized_markets[ISO3]):
            #   - search_volume: level + growth + stability (its monthly_searches
            #     array already carries the trailing 12 months).
            #   - keywords_for_keywords: related keywords for the intent score.
            # Both are current-month collections (no per-period series to
            # re-collect). location is the ISO2 Google Ads expects.
            market = (entity.localized_markets or {}).get(country_iso3) if entity is not None else None
            search_terms = as_term_list((market or {}).get("search_terms")) or as_term_list(
                (market or {}).get("primary_name")
            )
            location = country.iso2 if country is not None else None
            language = (country.search_language if country is not None else None) or "en"
            if search_terms and location is not None:
                gaps.append(
                    CoverageGap(
                        source=DataSource.PYTRENDS,
                        entity_key=entity_key,
                        country=country_iso3,
                        indicator_code="search_volume",
                        period_key=current_month_period_key(),
                        collection_params={"location": location, "language": language, "keywords": search_terms},
                    )
                )
                gaps.append(
                    CoverageGap(
                        source=DataSource.PYTRENDS,
                        entity_key=entity_key,
                        country=country_iso3,
                        indicator_code="keywords_for_keywords",
                        period_key=current_month_period_key(),
                        collection_params={
                            "location": location,
                            "language": language,
                            "seed_keywords": search_terms[:20],
                        },
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
