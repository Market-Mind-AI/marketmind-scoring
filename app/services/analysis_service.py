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

import json
import uuid
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import logger
from app.db.models.indicator_value import IndicatorValue
from app.db.models.mos_result import MosResult
from app.domain.enums import AvailabilityStatus, DataSource, IndicatorCode
from app.repositories.analysis_repository import AnalysisRepository
from app.repositories.country_repository import CountryRepository
from app.repositories.indicator_repository import IndicatorRepository
from app.repositories.mos_repository import MosRepository
from app.repositories.product_repository import ProductRepository
from app.schemas.coverage import CoverageGap, JobTaskResult
from app.services.catalog_service import _merge_localized_markets
from app.services.coverage_service import CoverageService
from app.services.enrichment_client import EnrichmentClient, EnrichmentError
from app.services.explanation_client import ExplanationClient
from app.services.indicator_engine import IndicatorEngine
from app.services.indicators import pytrends as pytrends_indicators
from app.services.macro_coverage import macro_gaps
from app.services.mos_engine import MosEngine
from app.services.product_identification_service import _fallback_market, _has_terms, _market_to_dict
from app.services.reference_market_client import ReferenceMarketClient, ReferenceMarketError
from app.services.weight_calibration_service import WeightCalibrationService
from app.utils.period import current_month_period_key
from app.utils.text import as_term_list

# The reference-market shortlist is a ranked list from the LLM (biggest markets
# first), and every candidate beyond the requested countries costs a full
# collection plus a MOS computation of its own. Eight candidates on one product
# turned a four-country analysis into nine MOS computations.
#
# Three consumers read this list, and they do NOT all want the same set:
#
#   resolve_demand_ceiling       the raw ranked top-N, requested countries
#                                included, because the ruler must not depend on
#                                what was asked for.
#   ensure_candidates_coverage   the top-N the caller did not request — what
#   score_reference_candidates   actually gets collected and scored.
#
# Those last two must agree exactly. When they did not — one capped, one not —
# scoring reached candidates whose demand had never been collected, and each
# produced a mos_results row reading "Missing mandatory indicator(s):
# trend_score, growth_score, ...". Correct rows, correctly marked unavailable,
# for work that should never have been attempted.
#
# Hence two named helpers instead of slices written out at each call site. Three
# hand-written slices are what allowed them to disagree, and the comment that
# used to sit here claiming the bound was "applied at BOTH candidate loops" was
# itself wrong about how many loops there were.
_MAX_REFERENCE_CANDIDATES = 3


def _top_reference_candidates(entity: Any) -> list[dict[str, Any]]:
    """The bounded shortlist for the DEMAND CEILING.

    Truncates the RAW ranked list, before any requested-country filtering, and
    that ordering is the point. The ceiling has to be query-independent — a
    country must score the same analysed alone or alongside twenty others, which
    is what makes MOS values comparable between analyses at all. Filtering by the
    request first would make the ruler depend on what was asked for.

    Use _champion_candidates for anything that should skip requested countries.
    """
    return list(entity.reference_candidates or [])[:_MAX_REFERENCE_CANDIDATES]


def _champion_candidates(entity: Any, requested: list[str]) -> list[dict[str, Any]]:
    """The bounded shortlist for the OFF-REQUEST CHAMPION, and for collecting it.

    Same bound, different set: the highest-ranked candidates the caller did NOT
    request. Filtering before truncating is correct here and wrong for the
    ceiling, because nothing downstream of this feeds back into the ruler — the
    ceiling is resolved first, from its own list.

    Sharing one list with the ceiling is what left the champion feature
    effectively dead: for an air fryer the ranked candidates are USA, IND, BRA,
    and a caller asking about USA and BRA got a single usable candidate out of
    three. The common case — asking about a product's actual big markets — is
    exactly the case where the raw top-N is all requested.

    Also what the coverage pass collects for, and that is sufficient rather than a
    third set: the non-requested members of the raw top-N are by construction the
    earliest non-requested candidates, so they are already in here.
    """
    requested_set = {c.upper() for c in requested}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for cand in entity.reference_candidates or []:
        iso3 = (cand.get("country") or "").upper()
        if not iso3 or iso3 in requested_set or iso3 in seen:
            continue
        seen.add(iso3)
        out.append(cand)
        if len(out) == _MAX_REFERENCE_CANDIDATES:
            break
    return out



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
        self._mos_repo = MosRepository(session)
        self._coverage_service = CoverageService(session)
        self._indicator_engine = IndicatorEngine(session)
        self._mos_engine = MosEngine(session)
        self._weight_calibration = WeightCalibrationService(session)
        self._explanation_client = ExplanationClient(session)
        self._enrichment_client = EnrichmentClient()
        self._reference_market_client = ReferenceMarketClient()

    async def run(self, request_id: uuid.UUID) -> None:
        """Hand the analysis to the agent, and get out of the way.

        The agent owns the whole lifecycle now: it marks the request running,
        reports progress, completes it with the scored markets, attaches the
        narrative, and fails it with a reason. This does not duplicate any of
        that -- it starts the run and watches the stream so a transport failure
        has somewhere to be recorded.

        An earlier version read the stream for scored markets and completed the
        request itself. That was wrong in a way that would have been hard to
        see: the scoring step emits its markets several steps before the
        champion is crowned and the evidence is assembled, so the request would
        have been marked done -- and billed -- carrying a partial answer, while
        the agent was still working on the rest of it.
        """
        analysis_request = await self._analysis_repo.get(request_id)
        if analysis_request is None:
            logger.warning(f"AnalysisRequest {request_id} not found")
            return

        payload = {"analysis_id": str(request_id)}

        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "POST", f"{settings.orchestrator_api_base_url}/run-analysis", json=payload
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        event = json.loads(line[6:])
                        if "error" in event:
                            # The agent reports its own analysis failures through
                            # the record. An error on the stream is the run
                            # itself coming apart, which is this side's to log.
                            logger.error(f"AnalysisRequest {request_id}: orchestrator error: {event['error']}")
                        else:
                            logger.info(f"AnalysisRequest {request_id}: {', '.join(event.keys())}")

        except Exception as exc:  # noqa: BLE001 — background task boundary, must never crash the process
            # Only reached when the orchestrator could not be talked to at all.
            # Anything the agent itself decided it has already recorded, so this
            # never overwrites a request it already finished.
            logger.exception(f"AnalysisRequest {request_id} could not be handed to the orchestrator")
            await self._session.rollback()
            current = await self._analysis_repo.get(request_id)
            if current is not None and current.status not in ("done", "failed"):
                await self._analysis_repo.mark_failed(
                    current, error_message=f"The orchestrator could not be reached: {exc}"
                )
                await self._session.commit()

    async def _search_language(self, iso3: str) -> str | None:
        country = await self._country_repo.get_by_iso3(iso3)
        return country.search_language if country is not None else None

    async def ensure_product_enrichment(
        self, entity: Any, countries: list[str], *, com_candidatos: bool = True
    ) -> None:
        """Fill the entity's per-country search vocabulary + reference candidates,
        calling the LLM ONLY for what's missing (a country without terms; empty
        reference_candidates). If the base already covers everything, no LLM call
        is made — reuse it (faster + cheaper). Merges (never overwrites).

        `com_candidatos=False` para a entidade-família: os candidatos existem
        para eleger o teto da régua de Demand Volume, e a categoria não é
        pontuada — pedi-los ali seria uma chamada de LLM paga por um número que
        ninguém usa."""
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

        if com_candidatos and not entity.reference_candidates:
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

    async def ensure_coverage(
        self, *, country_iso3: str, entity_key: uuid.UUID | None, terms_used: dict[str, Any] | None = None
    ) -> list[JobTaskResult]:
        """Checks (read-only) the last 6 monthly period_keys for every gap
        this country/product needs, and only dispatches collection
        (check_or_collect) for the ones actually missing.

        Devolve o resultado das tasks despachadas — vazio quando nada precisou
        ser coletado. É esse resultado que distingue as duas causas de um país
        continuar sem indicador (ver `_completar_paises`): task FALHADA é fonte
        fora do ar, e vale insistir; coleta que terminou sem falha e mesmo assim
        não produziu o indicador é fonte que não tem aquele dado."""
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
            # Os termos GENÉRICOS da família viajam na mesma coleta (decisão D4).
            # `search_volume` aceita 1000 keywords por task, então medir a
            # categoria ao lado da marca não custa uma request nova; os seeds de
            # `keywords_for_keywords`, que só aceitam 20, são divididos 14/6 pelo
            # MESMO construtor que o indicator_engine usa para ler — rótulos
            # diferentes deixariam o intent órfão.
            category_terms: list[str] = []
            if entity is not None and entity.generic_entity_key is not None:
                familia = await self._product_repo.get_by_entity_key(entity.generic_entity_key)
                if familia is not None:
                    mercado_familia = (familia.localized_markets or {}).get(country_iso3) or {}
                    category_terms = as_term_list(mercado_familia.get("search_terms")) or as_term_list(
                        mercado_familia.get("primary_name")
                    )
            location = country.iso2 if country is not None else None
            language = (country.search_language if country is not None else None) or "en"
            # What this country is actually searched for, recorded for the
            # caller. It is the one part of the pipeline a model wrote, and
            # nothing downstream reveals it: a wrong native term produces a
            # perfectly formed analysis of the wrong words.
            if terms_used is not None:
                terms_used.update(
                    {
                        "country": country_iso3,
                        "location": location,
                        "language": language,
                        "search_terms": list(search_terms),
                        "category_terms": list(category_terms),
                    }
                )
            if search_terms and location is not None:
                # Term-incremental demand ("só baixa se um termo novo aparece"):
                # collect ONLY the search terms that don't yet have volume data —
                # already-cached terms are never re-downloaded. Nothing missing =>
                # no search_volume collection at all.
                termos_para_volume = [*search_terms, *category_terms]
                missing_sv = await pytrends_indicators.missing_volume_terms(termos_para_volume, location)
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
                seed_terms = pytrends_indicators.build_seed_terms(search_terms, category_terms)
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

        # Os gaps macro vêm do construtor ÚNICO (app/services/macro_coverage.py):
        # quem coleta durante a análise e quem pré-aquece fora dela precisam
        # produzir exatamente a mesma lista, senão o pré-aquecimento esquenta
        # uma coisa e a análise pede outra.
        gaps.extend(macro_gaps(country_iso3))

        if not gaps:
            return []

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
        if not missing:
            return []
        _job_id, resultados = await self._coverage_service.check_or_collect(missing, requested_by="analysis_service")
        return resultados

    @staticmethod
    def _demand_gap_needs_recollect(current: dict[str, IndicatorValue], collection_code: str) -> bool:
        """True when a demand collection must (re-)run: any computed indicator it
        feeds is still missing/unavailable for this country/period. Keeps the
        analysis filling exactly the gaps a previous run left empty, without
        re-collecting what is already usable."""
        outputs = _DEMAND_COLLECTION_OUTPUTS.get(collection_code, ())
        return any(not _is_available(current, code) for code in outputs)

    async def ensure_candidates_coverage(self, entity: Any, requested: list[str], *, period_key: str) -> None:
        """Collect each reference-market candidate's FULL demand — search_volume
        AND keywords_for_keywords — exactly like a requested country, so its
        volume is measured through the identical pipeline (including the related-
        keyword expansion) and the ruler stays comparable. Collected regardless
        of the requested set (so the ruler is query-independent) and persisted /
        reused via the coverage cache. Candidates that ARE requested countries
        are skipped here — already collected under their own localized terms
        (pulled once)."""
        gaps: list[CoverageGap] = []
        for cand in _champion_candidates(entity, requested):
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
            seed_terms = pytrends_indicators.build_seed_terms(terms)
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

    async def resolve_demand_ceiling(
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
        for cand in _top_reference_candidates(entity):
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
                terms,
                country.iso2,
                period_key,
                seed_terms=pytrends_indicators.build_seed_terms(terms),
                language=country.search_language,
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

    async def score_reference_candidates(
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
        for cand in _champion_candidates(entity, requested):
            iso3 = cand.get("country")
            terms = as_term_list(cand.get("search_terms"))
            if not (iso3 and terms) or iso3 in requested_set or iso3 in seen:
                continue
            seen.add(iso3)
            country = await self._country_repo.get_by_iso3(iso3)
            if country is None or country.iso2 is None:
                continue
            try:
                await self.ensure_coverage(country_iso3=iso3, entity_key=entity_key)
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
        return await self._mos_engine.compute_mos(entity_key=entity_key, countries=attempted, period_key=period_key)
