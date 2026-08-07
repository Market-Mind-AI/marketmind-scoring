"""Fase 5 — POST /analyze orchestration. Reuses CoverageService (Fase 1),
IndicatorEngine (Fase 3), and MosEngine (Fase 4) exactly as they are — this
module only sequences them, it never duplicates their logic. Runs inside a
FastAPI BackgroundTask (see app/api/routes/analyze.py) — v1, no dedicated
worker/queue yet (user decision 2026-07-08, see memory
marketmind_mos_engine): if the API process restarts mid-run, the
AnalysisRequest is left stuck in "running" with no resume.

Freshness, not history: the final answer is always the current month's MOS per
country, same contract as POST /mos/compute. Coverage used to track the last 6
calendar months for the country-level macro sources, which turned out to mean
"re-download everything on the first analysis of each month" — those now use a
single row with a 90-day TTL (see COUNTRY_LEVEL_PERIOD_KEY and
settings.country_indicator_ttl_days).
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import MarketMindCoreError
from app.core.logging import logger
from app.db.models.indicator_value import IndicatorValue
from app.db.models.mos_result import MosResult
from app.domain.enums import AvailabilityStatus, DataSource, IndicatorCode
from app.repositories.analysis_repository import AnalysisRepository
from app.repositories.country_repository import CountryRepository
from app.repositories.indicator_repository import IndicatorRepository
from app.repositories.product_repository import ProductRepository
from app.schemas.coverage import CoverageGap
from app.schemas.mos import MosResultResponse
from app.services.catalog_service import _merge_localized_markets
from app.services.coverage_service import CoverageService
from app.services.enrichment_client import EnrichmentClient, EnrichmentError
from app.services.explanation_client import ExplanationClient
from app.services.indicator_engine import IndicatorEngine
from app.services.indicators import pytrends as pytrends_indicators
from app.services.indicators.oecd import RAW_INDICATOR_CODES as OECD_RAW_CODES
from app.services.indicators.undata import RAW_INDICATOR_CODES as UNDATA_RAW_CODES
from app.services.indicators.worldbank import RAW_INDICATOR_CODES as WORLDBANK_RAW_CODES
from app.services.mos_engine import MosEngine
from app.services.product_identification_service import _fallback_market, _has_terms, _market_to_dict
from app.services.reference_market_client import ReferenceMarketClient, ReferenceMarketError
from app.services.weight_calibration_service import WeightCalibrationService
from app.utils.period import COUNTRY_LEVEL_PERIOD_KEY, current_month_period_key
from app.utils.text import as_term_list

# The reference-market shortlist is a ranked list from the LLM (biggest markets
# first), and every candidate beyond the requested countries costs a full
# collection + MOS computation of its own. Six candidates quadrupled the work of
# a two-country analysis. Three keeps the ruler meaningful — it only needs the
# biggest real search volume among plausible big markets — at a quarter of the
# cost. Applied at BOTH candidate loops, and it has to stay that way: the demand
# ceiling reads data that only the scoring loop collects, so measuring a
# candidate the scoring loop skipped would read a series that was never fetched.
_MAX_REFERENCE_CANDIDATES = 3
# How much history to ASK each annual/quarterly source for (WorldBank/OECD/
# UNData) — generous on purpose so formulas needing several years (GDP Growth,
# Real Estate Growth, ...) have enough data. Unrelated to coverage freshness,
# which is a separate question answered by country_indicator_ttl_days: this one
# is "how far back to fetch", that one is "when to fetch again".
_ANNUAL_LOOKBACK_YEARS = 10

# Raw PyTrends collection -> the computed demand indicators it feeds (Cap. 4
# §4.2). Coverage is tracked per raw collection, but "covered" must mean "the
# indicator is usable", NOT merely "a collection run finished": a run can
# succeed yet leave its indicator unavailable (e.g. related keywords returned
# with no search volume). So a demand gap is force-recollected whenever ANY
# indicator it feeds is still missing/unavailable — the analysis always retries
# exactly the countries/signals that lack data instead of skipping them as
# already covered (user 2026-07-22: "o grande problema foi não tentar coletar
# os outros países").
_DEMAND_COLLECTION_OUTPUTS: dict[str, tuple[IndicatorCode, ...]] = {
    "search_volume": (
        IndicatorCode.TREND_SCORE,
        IndicatorCode.GROWTH_SCORE,
        IndicatorCode.DEMAND_STABILITY_SCORE,
    ),
    "keywords_for_keywords": (IndicatorCode.SEARCH_INTENT_SCORE,),
}


def _is_available(values: dict[str, IndicatorValue], code: IndicatorCode) -> bool:
    iv = values.get(code.value)
    return (
        iv is not None
        and iv.normalized_score is not None
        and iv.availability_status != AvailabilityStatus.UNAVAILABLE.value
    )


class AnalysisService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._analysis_repo = AnalysisRepository(session)
        self._product_repo = ProductRepository(session)
        self._country_repo = CountryRepository(session)
        self._indicator_repo = IndicatorRepository(session)
        self._coverage_service = CoverageService(session)
        self._indicator_engine = IndicatorEngine(session)
        self._mos_engine = MosEngine(session)
        self._weight_calibration = WeightCalibrationService(session)
        self._explanation_client = ExplanationClient(session)
        self._enrichment_client = EnrichmentClient()
        self._reference_market_client = ReferenceMarketClient()

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

            # Enrichment deferred from identify-image (kept fast). Generate the
            # per-country search vocabulary + reference candidates HERE, in the
            # background — but ONLY for what's missing: a country without terms
            # triggers enrichment for it, empty reference_candidates trigger the
            # candidates call. If the base already has everything, NO LLM call is
            # made (reuse → faster + cheaper). Must run before coverage, which
            # reads the entity's terms.
            if entity_key is not None:
                entity_to_enrich = await self._product_repo.get_by_entity_key(entity_key)
                if entity_to_enrich is not None:
                    await self._ensure_product_enrichment(entity_to_enrich, countries)
                    await self._session.commit()

            for country_iso3 in countries:
                await self._ensure_coverage(country_iso3=country_iso3, entity_key=entity_key)

            period_key = current_month_period_key()

            # Demand Volume ruler (Cap. 4 §4.2.1, 2026-07-22): the ceiling is the
            # LARGEST real search volume among the product's reference-market
            # CANDIDATES (a ranked shortlist the enrichment LLM proposed). Each
            # candidate is collected here (reusing cached data) regardless of the
            # requested countries, so the ruler is stable / query-independent and
            # a wrong single LLM guess can't skew it. Falls back to the largest
            # requested-country volume, then to the old fixed 2M bound — the mode
            # is carried into the final narrative so the LLM states which ruler
            # was used.
            entity = await self._product_repo.get_by_entity_key(entity_key) if entity_key is not None else None
            demand_ceiling: float | None = None
            demand_ruler: dict[str, Any] | None = None
            if entity is not None:
                await self._ensure_candidates_coverage(entity, countries, period_key=period_key)
                demand_ceiling, demand_ruler = await self._resolve_demand_ceiling(entity, countries, period_key)

            for country_iso3 in countries:
                await self._indicator_engine.compute_all(
                    country_iso3=country_iso3,
                    entity_key=entity_key,
                    period_key=period_key,
                    demand_ceiling=demand_ceiling,
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

            # Reference "champion" (2026-07-27): the product's best-MOS market
            # among the enrichment LLM's reference candidates. MOS uses fixed
            # 100/0 TOPSIS anchors, so it is query-independent — scoring the
            # candidates never changes the requested countries' scores. Their
            # demand is already collected (the ruler), so this adds only free
            # World Bank/OECD/UN data + compute (~zero paid cost). If the best
            # market wasn't requested, we surface it (crowned) so the user can
            # benchmark their picks against the product's standout market —
            # useful even for a single-country request.
            champion_country: str | None = None
            requested_set = set(countries)
            if entity is not None and entity_key is not None and mos_results:
                candidate_results = await self._score_reference_candidates(
                    entity=entity, requested=countries, period_key=period_key, demand_ceiling=demand_ceiling
                )
                scored_all = [r for r in [*mos_results, *candidate_results] if r.mos is not None]
                if scored_all:
                    champion = max(scored_all, key=lambda r: float(r.mos))
                    champion_country = champion.country
                    if champion.country not in requested_set:
                        mos_results = [*mos_results, champion]  # off-request → surface for benchmark

            serialized = []
            for r in mos_results:
                row = MosResultResponse.model_validate(r, from_attributes=True).model_dump(mode="json")
                row["requested"] = r.country in requested_set
                row["is_champion"] = champion_country is not None and r.country == champion_country
                serialized.append(row)
            # Evidence packet (deterministic) + final LLM narrative (best-effort).
            # The packet — glossary, weights, demand ruler, and per country the MOS
            # + dimension subscores + every scored signal with its explanation — is
            # persisted (breakdown) so the frontend renders the rich MOS modal from
            # one GET. The narrative turns that packet into prose via the local
            # Ollama service, and now also answers the buyer's own free-text prompt
            # (strictly from the packet). Neither ever blocks or fails the analysis.
            explanation: str | None = None
            breakdown: dict[str, Any] | None = None
            if entity_key is not None and mos_results:
                breakdown = await self._explanation_client.build_evidence(
                    entity_key=entity_key,
                    mos_results=mos_results,
                    period_key=period_key,
                    demand_ruler=demand_ruler,
                )
                if breakdown is not None:
                    # Tag which markets were requested vs the off-request champion
                    # so both the narrative and the frontend can crown/benchmark it.
                    for c in breakdown.get("countries") or []:
                        c["requested"] = c.get("country") in requested_set
                        c["is_champion"] = champion_country is not None and c.get("country") == champion_country
                    breakdown["champion_country"] = champion_country
                    breakdown["requested_countries"] = list(countries)
                    explanation = await self._explanation_client.narrate(
                        packet=breakdown,
                        product_name=analysis_request.canonical_title or "",
                        user_prompt=analysis_request.user_prompt,
                    )
            await self._analysis_repo.mark_done(
                analysis_request, result=serialized, explanation=explanation, breakdown=breakdown
            )
            await self._session.commit()
        except Exception as exc:  # noqa: BLE001 — background task boundary, must never crash the process
            logger.exception(f"AnalysisRequest {request_id} failed")
            await self._session.rollback()
            failed_request = await self._analysis_repo.get(request_id)
            if failed_request is not None:
                await self._analysis_repo.mark_failed(failed_request, error_message=str(exc))
                await self._session.commit()

    async def run_via_orchestrator(self, request_id: uuid.UUID) -> None:
        """Fase 5 (New Architecture) — proxies the analysis execution to the LangGraph orchestrator.
        Connects via SSE, maps node completion events to DB status, and writes the final report."""
        import json
        import httpx
        from app.core.config import settings
        
        analysis_request = await self._analysis_repo.get(request_id)
        if analysis_request is None:
            logger.warning(f"AnalysisRequest {request_id} not found")
            return
            
        try:
            await self._analysis_repo.mark_running(analysis_request)
            await self._session.commit()
            
            payload = {
                "analysis_id": str(request_id),
                "product_description": analysis_request.user_prompt or (analysis_request.canonical_title or ""),
                "product_image_url": "",
                "attributes": {},
                "enrichment_results": [],
                "mos_score": {},
                "report": {},
                "errors": []
            }
            
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("POST", f"{settings.orchestrator_api_base_url}/run-analysis", json=payload) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            data = json.loads(line[6:])
                            logger.info(f"Orchestrator event received: {list(data.keys())}")
                            
                            if "report" in data and "narrative" in data["report"]:
                                await self._analysis_repo.mark_done(
                                    analysis_request,
                                    result=[],
                                    explanation=data["report"]["narrative"],
                                    breakdown={"status": "done via orchestrator"}
                                )
                                await self._session.commit()
                                break
        except Exception as exc:
            logger.exception(f"AnalysisRequest {request_id} failed via orchestrator")
            await self._session.rollback()
            failed_request = await self._analysis_repo.get(request_id)
            if failed_request is not None:
                await self._analysis_repo.mark_failed(failed_request, error_message=str(exc))
                await self._session.commit()


    async def _search_language(self, iso3: str) -> str | None:
        country = await self._country_repo.get_by_iso3(iso3)
        return country.search_language if country is not None else None

    async def _ensure_product_enrichment(self, entity: Any, countries: list[str]) -> None:
        """Fill the entity's per-country search vocabulary + reference candidates,
        calling the LLM ONLY for what's missing (a country without terms; empty
        reference_candidates). If the base already covers everything, no LLM call
        is made — reuse it (faster + cheaper). Merges (never overwrites)."""
        product_name = entity.canonical_title or ""

        missing = [c for c in countries if not _has_terms((entity.localized_markets or {}).get(c))]
        if missing:
            try:
                fill = [(iso3, await self._search_language(iso3)) for iso3 in missing]
                markets = await self._enrichment_client.localized_markets(
                    product_name=product_name, brand=None, category=entity.category, countries=fill
                )
                incoming = {
                    m.country: _market_to_dict(m)
                    for m in markets
                    if m.country in set(missing) and _has_terms(_market_to_dict(m))
                }
                if incoming:
                    entity.localized_markets = _merge_localized_markets(entity.localized_markets, incoming)
            except EnrichmentError as exc:
                logger.warning(f"Background localized_markets enrichment failed, using deterministic fallback: {exc}")
            # Deterministic fallback so coverage/compute never strand a country.
            current_lm = dict(entity.localized_markets or {})
            still_missing = [c for c in countries if not _has_terms(current_lm.get(c))]
            if still_missing:
                for iso3 in still_missing:
                    current_lm[iso3] = _fallback_market(iso3, product_name, None)
                entity.localized_markets = current_lm

        if not entity.reference_candidates:
            try:
                supported = [c.iso3 for c in await self._country_repo.list_all()]
                supported_set = set(supported)
                candidates: list[dict[str, Any]] = []
                seen: set[str] = set()
                for cand in await self._reference_market_client.candidates(
                    product_name=product_name, brand=None, category=entity.category, supported_countries=supported
                ):
                    entry = _market_to_dict(cand)
                    if cand.country in supported_set and cand.country not in seen and _has_terms(entry):
                        candidates.append({"country": cand.country, **entry})
                        seen.add(cand.country)
                if candidates:
                    entity.reference_candidates = candidates
            except ReferenceMarketError as exc:
                logger.warning(f"Background reference-market candidates unavailable: {exc}")

    async def _ensure_coverage(self, *, country_iso3: str, entity_key: uuid.UUID | None) -> None:
        """Checks (read-only) the last 6 monthly period_keys for every gap
        this country/product needs, and only dispatches collection
        (check_or_collect) for the ones actually missing."""
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
                # Term-incremental demand ("só baixa se um termo novo aparece"):
                # collect ONLY the search terms that don't yet have volume data —
                # already-cached terms are never re-downloaded. Nothing missing =>
                # no search_volume collection at all.
                missing_sv = await pytrends_indicators.missing_volume_terms(search_terms, location)
                if missing_sv:
                    gaps.append(
                        CoverageGap(
                            source=DataSource.PYTRENDS,
                            entity_key=entity_key,
                            country=country_iso3,
                            indicator_code="search_volume",
                            period_key=current_month_period_key(),
                            collection_params={"location": location, "language": language, "keywords": missing_sv},
                            force=True,
                        )
                    )
                # Related keywords (intent) are a function of the seed SET, not of
                # individual terms, so re-collect only when the current seed's
                # related rows are absent — i.e. a genuinely new term entered the
                # top-20 seeds. A stable/merged seed set => cache hit => no download.
                seed_terms = search_terms[:20]
                if seed_terms and not await pytrends_indicators.related_rows_exist(seed_terms, location):
                    gaps.append(
                        CoverageGap(
                            source=DataSource.PYTRENDS,
                            entity_key=entity_key,
                            country=country_iso3,
                            indicator_code="keywords_for_keywords",
                            period_key=current_month_period_key(),
                            collection_params={"location": location, "language": language, "seed_keywords": seed_terms},
                            # force: we only reach here when the CURRENT seed has no
                            # related rows, so it MUST collect regardless of a stale
                            # "covered" coverage_index row from a prior seed set —
                            # otherwise search_intent_score is stranded → abort.
                            force=True,
                        )
                    )

        current_year = datetime.now(UTC).year
        start_year, end_year = current_year - _ANNUAL_LOOKBACK_YEARS, current_year

        # One coverage row per (country, indicator), not one per month.
        #
        # This used to loop over the last 6 calendar months, producing 6 × 12
        # WorldBank gaps per country. Because the coverage lookup matches on
        # period_key exactly, the first analysis of a new month found no row for
        # it and re-collected every macro indicator for every country — the TTL
        # never got a say. It is the reason one measured analysis of two
        # countries took 61 minutes, 91% of it re-downloading World Bank figures
        # that were identical to the ones already stored.
        #
        # These indicators are annual and read as "latest known value" (see
        # COUNTRY_LEVEL_PERIOD_KEY), so a single row with a 90-day TTL expresses
        # the real rule: don't re-download anything fetched in the last three
        # months, and when something does age out, re-download only that one.
        for codes in WORLDBANK_RAW_CODES.values():
            for code in codes:
                gaps.append(
                    CoverageGap(
                        source=DataSource.WORLDBANK,
                        entity_key=None,
                        country=country_iso3,
                        indicator_code=code,
                        period_key=COUNTRY_LEVEL_PERIOD_KEY,
                        ttl_days=settings.country_indicator_ttl_days,
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
                        period_key=COUNTRY_LEVEL_PERIOD_KEY,
                        ttl_days=settings.country_indicator_ttl_days,
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
                        period_key=COUNTRY_LEVEL_PERIOD_KEY,
                        ttl_days=settings.country_indicator_ttl_days,
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
        # A forced gap (its computed indicator is missing/unavailable) must be
        # collected even when its raw coverage row looks fresh — otherwise the
        # stale "covered" flag would strand a country whose indicator is empty.
        missing = [gap for gap, status in zip(unique_gaps, statuses, strict=True) if gap.force or not status.covered]
        if missing:
            await self._coverage_service.check_or_collect(missing, requested_by="analysis_service")

    @staticmethod
    def _demand_gap_needs_recollect(current: dict[str, IndicatorValue], collection_code: str) -> bool:
        """True when a demand collection must (re-)run: any computed indicator it
        feeds is still missing/unavailable for this country/period. Keeps the
        analysis filling exactly the gaps a previous run left empty, without
        re-collecting what is already usable."""
        outputs = _DEMAND_COLLECTION_OUTPUTS.get(collection_code, ())
        return any(not _is_available(current, code) for code in outputs)

    async def _ensure_candidates_coverage(self, entity: Any, requested: list[str], *, period_key: str) -> None:
        """Collect each reference-market candidate's FULL demand — search_volume
        AND keywords_for_keywords — exactly like a requested country, so its
        volume is measured through the identical pipeline (including the related-
        keyword expansion) and the ruler stays comparable. Collected regardless
        of the requested set (so the ruler is query-independent) and persisted /
        reused via the coverage cache. Candidates that ARE requested countries
        are skipped here — already collected under their own localized terms
        (pulled once)."""
        gaps: list[CoverageGap] = []
        for cand in (entity.reference_candidates or [])[:_MAX_REFERENCE_CANDIDATES]:
            iso3 = cand.get("country")
            terms = as_term_list(cand.get("search_terms"))
            if not (iso3 and terms) or iso3 in requested:
                continue
            country = await self._country_repo.get_by_iso3(iso3)
            if country is None or country.iso2 is None:
                continue
            language = country.search_language or "en"
            gaps.append(
                CoverageGap(
                    source=DataSource.PYTRENDS,
                    entity_key=entity.entity_key,
                    country=iso3,
                    indicator_code="search_volume",
                    period_key=current_month_period_key(),
                    collection_params={"location": country.iso2, "language": language, "keywords": terms},
                )
            )
            seed_terms = terms[:20]
            # Force collection when related rows are absent under THIS seed set
            # (a stale "covered" flag from a prior run under different seeds must
            # not strand the candidate's search_intent — same fix as requested
            # countries). Needed so the candidate can be fully scored for the
            # off-request "champion" benchmark, not just measured for the ruler.
            related_exists = await pytrends_indicators.related_rows_exist(seed_terms, country.iso2)
            gaps.append(
                CoverageGap(
                    source=DataSource.PYTRENDS,
                    entity_key=entity.entity_key,
                    country=iso3,
                    indicator_code="keywords_for_keywords",
                    period_key=current_month_period_key(),
                    collection_params={"location": country.iso2, "language": language, "seed_keywords": seed_terms},
                    force=not related_exists,
                )
            )
        if not gaps:
            return
        statuses = await self._coverage_service.check_coverage(gaps)
        # Honor gap.force (kfk with absent related rows) even when the raw coverage
        # row looks fresh — otherwise the stale flag strands search_intent.
        missing = [gap for gap, status in zip(gaps, statuses, strict=True) if gap.force or not status.covered]
        if missing:
            await self._coverage_service.check_or_collect(missing, requested_by="analysis_service:reference_candidates")

    async def _resolve_demand_ceiling(
        self, entity: Any, countries: list[str], period_key: str
    ) -> tuple[float | None, dict[str, Any]]:
        """The Demand Volume ruler ceiling + mode, for narration:
        - reference — the LARGEST real Google-search volume among the product's
          reference CANDIDATES (a ranked shortlist chosen by a real LLM at
          identify time). The candidate set is fixed per product and does NOT
          depend on which countries are requested, so the ruler — and therefore
          every country's Demand Volume score — is DETERMINISTIC / query-
          independent (a country scores the same analyzed alone or with 20
          others). The LLM only proposes plausible big markets; real search
          volume elects the biggest among them (consistent with the search-based
          metric — a sales-biggest market like China, barely on Google, won't
          dominate the ruler).
        - fixed — None (no candidate had volume), so the engine uses the 2M bound.
        Volumes are read from already-collected data via compute_trend_score."""
        measured: list[dict[str, Any]] = []
        for cand in (entity.reference_candidates or [])[:_MAX_REFERENCE_CANDIDATES]:
            iso3 = cand.get("country")
            if not iso3:
                continue
            # A candidate that is also requested was collected under its own
            # localized_markets terms (same coverage key); measure it with those
            # to avoid reading a never-collected reference-term series.
            market = (entity.localized_markets or {}).get(iso3, {}) if iso3 in countries else cand
            terms = as_term_list(market.get("search_terms"))
            country = await self._country_repo.get_by_iso3(iso3)
            if not (terms and country is not None and country.iso2 is not None):
                continue
            # Identical pipeline to the requested countries — including the
            # related-keyword expansion — so the ceiling is apples-to-apples.
            r = await pytrends_indicators.compute_trend_score(
                terms, country.iso2, period_key, seed_terms=terms[:20], language=country.search_language
            )
            if r.raw_value is not None and float(r.raw_value) > 0:
                measured.append({"country": iso3, "volume": float(r.raw_value)})
        if measured:
            winner = max(measured, key=lambda m: m["volume"])
            return winner["volume"], {
                "mode": "reference",
                "country": winner["country"],
                "volume": winner["volume"],
                "candidates": measured,
            }
        return None, {"mode": "fixed"}

    async def _score_reference_candidates(
        self, *, entity: Any, requested: list[str], period_key: str, demand_ceiling: float | None
    ) -> list[MosResult]:
        """Best-effort MOS for the product's reference-candidate markets the user
        did NOT request, so the best-MOS market (the "champion") can be surfaced
        for benchmark even when it wasn't selected. MOS uses fixed 100/0 TOPSIS
        anchors → query-independent, so this never changes the requested scores.
        The candidates' demand is already collected (the ruler); only free World
        Bank/OECD/UN data + compute remain, so ~zero paid cost. A candidate that
        can't be fully scored is simply skipped (never aborts the analysis)."""
        entity_key = entity.entity_key
        if entity_key is None:
            return []
        requested_set = set(requested)
        attempted: list[str] = []
        seen: set[str] = set()
        for cand in entity.reference_candidates or []:
            iso3 = cand.get("country")
            terms = as_term_list(cand.get("search_terms"))
            if not (iso3 and terms) or iso3 in requested_set or iso3 in seen:
                continue
            seen.add(iso3)
            country = await self._country_repo.get_by_iso3(iso3)
            if country is None or country.iso2 is None:
                continue
            try:
                await self._ensure_coverage(country_iso3=iso3, entity_key=entity_key)
                await self._indicator_engine.compute_all(
                    country_iso3=iso3,
                    entity_key=entity_key,
                    period_key=period_key,
                    demand_ceiling=demand_ceiling,
                    terms_override=terms,
                )
                attempted.append(iso3)
            except Exception as exc:  # noqa: BLE001 — benchmark is best-effort, never fails the analysis
                logger.warning(f"Reference candidate {iso3} could not be scored (skipped): {exc}")
        if not attempted:
            return []
        return await self._mos_engine.compute_mos(
            entity_key=entity_key, countries=attempted, period_key=period_key
        )
