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

import asyncio
import json
import uuid
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import MarketMindCoreError
from app.core.logging import logger
from app.db.models.analysis_request import AnalysisRequest
from app.db.models.indicator_value import IndicatorValue
from app.db.models.mos_result import MosResult
from app.domain.enums import AvailabilityStatus, DataSource, IndicatorCode, JobTaskStatus, PeriodType
from app.repositories.analysis_repository import AnalysisRepository
from app.repositories.country_repository import CountryRepository
from app.repositories.indicator_repository import IndicatorRepository
from app.repositories.mos_repository import MosRepository
from app.repositories.product_repository import ProductRepository
from app.schemas.coverage import CoverageGap, JobTaskResult
from app.schemas.mos import MosResultResponse
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
from app.utils.time import utc_now

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
        analysis_request = await self._analysis_repo.get(request_id)
        if analysis_request is None:
            logger.warning(f"AnalysisRequest {request_id} not found — background task has nothing to do")
            return

        try:
            await self._analysis_repo.mark_running(analysis_request)
            analysis_request.heartbeat_at = utc_now()
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
                    await self.ensure_product_enrichment(entity_to_enrich, countries)
                    # A CATEGORIA do produto também precisa de vocabulário: é ela
                    # que dá a escala contra a qual a demanda da marca é lida
                    # (decisão D4). Roda com `brand=None`, então produz termos
                    # genéricos, e é reaproveitada por TODAS as marcas dessa
                    # família — a segunda marca do mesmo produto não paga de novo.
                    # Sem candidatos de referência: a régua de demanda é da marca,
                    # a categoria é contexto e nunca é pontuada.
                    familia_key = entity_to_enrich.generic_entity_key
                    if familia_key is not None:
                        familia = await self._product_repo.get_by_entity_key(familia_key)
                        if familia is not None:
                            await self.ensure_product_enrichment(familia, countries, com_candidatos=False)
                    await self._session.commit()

            for country_iso3 in countries:
                await self.ensure_coverage(country_iso3=country_iso3, entity_key=entity_key)
                # Sinal de vida por país: é o passo mais longo entre dois
                # batimentos, e é o que a varredura usa para distinguir uma
                # análise ocupada de uma que morreu com o pod.
                await self._bater(analysis_request)

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
            # A champion computed for this period is reusable as-is, and that is
            # what makes a repeat analysis fast: candidate demand is the slowest
            # collection in the system, and re-running it produces the same
            # answer. Measured before this: three consecutive analyses of the
            # same product took 594s, 203s and 396s, with the four requested
            # countries collecting nothing on the repeats — all of it candidate
            # work, redone.
            champion_is_cached = entity is not None and entity.champion_period_key == period_key

            if entity is not None:
                if not champion_is_cached:
                    await self.ensure_candidates_coverage(entity, countries, period_key=period_key)
                # The ceiling only READS candidate demand, it never collects. So
                # skipping the collection above leaves it reading exactly the
                # data the run that computed the champion left behind — the
                # ruler is therefore stable across repeats rather than drifting.
                demand_ceiling, demand_ruler = await self.resolve_demand_ceiling(entity, countries, period_key)

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

            # Completude (princípio do usuário de 2026-07-11): o MOS nunca sai
            # de dado parcial. Todo país pontuado carrega o mesmo conjunto de
            # indicadores obrigatórios, na mesma régua 0-100 — é isso que mantém
            # os scores comparáveis entre si.
            #
            # A regra de 25/08 era "esgotar o que dá e excluir o resto": duas
            # passadas de coleta, e o que continuasse incompleto saía do
            # ranking com motivo. Ela entregava um resultado a menos por uma
            # queda de fonte de 29 minutos (incidente do UN Data em 25/08, 9 de
            # 24 países), e o usuário paga por análise completa (decisão D3).
            #
            # A regra agora é: insistir enquanto a causa for TRANSITÓRIA, e só
            # aceitar país de fora quando a causa for DEFINITIVA. Ver
            # `_completar_paises` para como as duas são distinguidas.
            definitivos: dict[str, str] = {}
            if entity_key is not None:
                definitivos = await self._completar_paises(
                    analysis_request,
                    entity_key=entity_key,
                    countries=countries,
                    period_key=period_key,
                    demand_ceiling=demand_ceiling,
                )
                if definitivos:
                    detalhe = "; ".join(f"{c}: {motivo}" for c, motivo in definitivos.items())
                    logger.warning(f"Excluídos do MOS porque a fonte não tem o dado: {detalhe}")

            mos_results = (
                await self._mos_engine.compute_mos(entity_key=entity_key, countries=countries, period_key=period_key)
                if entity_key is not None
                else []
            )

            # Só agora se sabe se sobrou alguma coisa. Nenhum país pontuável é o
            # único caso em que não há mais o que fazer.
            if entity_key is not None and not any(r.mos is not None for r in mos_results):
                motivos = (
                    "; ".join(f"{r.country}: {r.excluded_reason or 'sem dado'}" for r in mos_results)
                    or "nenhum país avaliado"
                )
                raise MarketMindCoreError(
                    "Analysis aborted — no country could be scored, so there is no ranking to return. " + motivos
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
                champion_row: MosResult | None = None

                # The best requested market in THIS analysis. The cached
                # champion has to beat it to still be one.
                best_requested = max(
                    (r for r in mos_results if r.mos is not None),
                    key=lambda r: float(r.mos),
                    default=None,
                )

                if champion_is_cached:
                    # Cached, including the case where it is cached as "no
                    # candidate could be scored" (period set, country NULL) —
                    # that must not be retried on every analysis either.
                    cached_country = entity.champion_country
                    cached_mos = entity.champion_mos

                    # Re-run the comparison, which caching had quietly dropped.
                    # The uncached path computes max(requested + candidates); the
                    # cached path used to take the stored country as champion
                    # unconditionally, and that produced a visibly false claim:
                    # a champion stored at 66.37 (it had beaten the USA's 66.31
                    # by six hundredths) stayed crowned "top market for this
                    # product" in a later analysis where the USA scored 69.
                    #
                    # The crown means "beats everything you asked about". If a
                    # requested market beats it, there is nothing to surface —
                    # the caller already has the product's best market in their
                    # own list.
                    beats_requested = (
                        cached_country is not None
                        and cached_mos is not None
                        and (best_requested is None or float(cached_mos) > float(best_requested.mos))
                    )

                    if beats_requested:
                        champion_country = cached_country
                        if cached_country not in requested_set:
                            champion_row = await self._mos_repo.get_mos_result(
                                entity_key, cached_country, PeriodType.MONTH.value, period_key
                            )
                            if champion_row is None:
                                # The product says it has a champion but its MOS
                                # row is gone. Recompute rather than report a
                                # country with no numbers behind it.
                                champion_is_cached = False
                                champion_country = None
                    else:
                        # The stored champion lost. Crown the best requested
                        # market instead, and append nothing.
                        champion_country = best_requested.country if best_requested is not None else None

                        # And promote it, rather than only displaying it. The
                        # stored value means "this product's best known market",
                        # so leaving a beaten champion in place would keep it
                        # wrong for every later analysis: a request for ESP and
                        # PRT alone would load BRA at 66.37 and crown it, while
                        # the USA at 69.03 was already known to be better.
                        #
                        # Cheap — the numbers are already in hand, and no
                        # candidate collection is involved.
                        if best_requested is not None:
                            await self._product_repo.set_champion(
                                entity,
                                country=best_requested.country,
                                mos=best_requested.mos,
                                period_key=period_key,
                            )
                            await self._session.commit()

                if not champion_is_cached:
                    candidate_results = await self.score_reference_candidates(
                        entity=entity, requested=countries, period_key=period_key, demand_ceiling=demand_ceiling
                    )
                    scored_all = [r for r in [*mos_results, *candidate_results] if r.mos is not None]
                    if scored_all:
                        champion = max(scored_all, key=lambda r: float(r.mos))
                        champion_country = champion.country
                        if champion.country not in requested_set:
                            champion_row = champion
                    # Persisted whether or not a champion was found: the period
                    # is what marks the work as done, so "nothing scorable this
                    # month" is recorded rather than re-attempted.
                    await self._product_repo.set_champion(
                        entity,
                        country=champion_country,
                        mos=champion.mos if champion_country is not None else None,
                        period_key=period_key,
                    )
                    await self._session.commit()

                if champion_row is not None:
                    mos_results = [*mos_results, champion_row]  # off-request → surface for benchmark

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
            breakdown: dict[str, Any] | None = None
            explanation_packet: dict[str, Any] | None = None
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
                    # Narrated after the answer is persisted, not here.
                    explanation_packet = breakdown
            # The answer lands here, without waiting for the narrative.
            #
            # The evidence packet above is deterministic and fast; the narrative
            # is a single LLM call and it dominated everything. Measured on the
            # deployed environment: an analysis that collected NOTHING — every
            # macro indicator cached, every demand series cached, the champion
            # reused — still took 192s, and timing the narration alone on the
            # same packet gave 249s. There was no data left to optimise; there
            # was a synchronous LLM call between a finished MOS and the caller.
            #
            # So the request completes with its scores and breakdown, and the
            # text is attached a moment later. Note the consequence for the
            # frontend: GET /analyze/{id} returns done with explanation=null for
            # a short window, and the SSE stream closes on the terminal status,
            # so the narrative arrives on a subsequent read rather than being
            # pushed. explanation has always been nullable and best-effort, so
            # this widens an existing case rather than inventing one.
            await self._analysis_repo.mark_done(
                analysis_request, result=serialized, explanation=None, breakdown=breakdown
            )
            await self._session.commit()

            if explanation_packet is not None:
                # The whole block, not just the narrate() call. The analysis is
                # already committed as done at this point, so ANY failure past
                # here — the LLM, the write, the commit — must be swallowed. An
                # earlier version wrapped only narrate(), and a failure in the
                # write escaped to the outer handler and marked an
                # already-successful analysis as failed. Caught by a test.
                try:
                    narrative = await self._explanation_client.narrate(
                        packet=explanation_packet,
                        product_name=analysis_request.canonical_title or "",
                        user_prompt=analysis_request.user_prompt,
                    )
                    await self._analysis_repo.set_explanation(analysis_request, narrative)
                    await self._session.commit()
                except Exception:  # noqa: BLE001 — the answer is already delivered; nothing here may undo it
                    logger.exception(f"AnalysisRequest {request_id} is done, but attaching its narrative failed")
                    await self._session.rollback()
        except Exception as exc:  # noqa: BLE001 — background task boundary, must never crash the process
            logger.exception(f"AnalysisRequest {request_id} failed")
            await self._session.rollback()
            failed_request = await self._analysis_repo.get(request_id)
            if failed_request is not None:
                await self._analysis_repo.mark_failed(failed_request, error_message=str(exc))
                await self._session.commit()

    async def run_via_orchestrator(self, request_id: uuid.UUID) -> None:
        """Run the analysis through the LangGraph orchestrator instead of `run`.

        Not finished: the orchestrator does not return scored markets yet (it is
        wired to the scoring tools in step 8 of the migration). Until it does,
        this path can only fail, and failing is the point.

        What stood here marked the request `done` the moment the orchestrator
        sent any narrative, with `result=[]` and a placeholder breakdown. So
        flipping USE_NEW_ORCHESTRATOR handed the buyer an analysis with no
        countries, no MOS and no evidence, recorded as complete -- and billing
        is tied to `done` (D8). An empty answer that bills is worse than no
        answer, so the rule here is the same as everywhere else in this service:
        nothing partial goes out.
        """
        analysis_request = await self._analysis_repo.get(request_id)
        if analysis_request is None:
            logger.warning(f"AnalysisRequest {request_id} not found")
            return

        try:
            await self._analysis_repo.mark_running(analysis_request)
            await self._session.commit()

            payload = {
                "analysis_id": str(request_id),
                "countries": list(analysis_request.countries),
                "product_description": analysis_request.user_prompt or (analysis_request.canonical_title or ""),
                "product_image_url": "",
                "attributes": {},
                "enrichment_results": [],
                "mos_score": {},
                "report": {},
                "errors": [],
            }

            scored: list[dict[str, Any]] = []
            stream_error: str | None = None

            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "POST", f"{settings.orchestrator_api_base_url}/run-analysis", json=payload
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data = json.loads(line[6:])
                        logger.info(f"Orchestrator event received: {list(data.keys())}")
                        if "error" in data:
                            stream_error = str(data["error"])
                        # Step 8 fills this in: the terminal event carries the
                        # scored markets, and only those may complete a request.
                        for node_output in data.values():
                            if isinstance(node_output, dict) and node_output.get("mos_results"):
                                scored = node_output["mos_results"]

            if not scored:
                raise MarketMindCoreError(
                    "Orchestrator run produced no scored market, so there is nothing to deliver. "
                    + (stream_error or "The orchestrator is not wired to the scoring service yet (migration step 8).")
                )

            await self._analysis_repo.mark_done(
                analysis_request, result=scored, explanation=None, breakdown=None
            )
            await self._session.commit()

        except Exception as exc:  # noqa: BLE001 - background task boundary, must never crash the process
            logger.exception(f"AnalysisRequest {request_id} failed via orchestrator")
            await self._session.rollback()
            failed_request = await self._analysis_repo.get(request_id)
            if failed_request is not None:
                await self._analysis_repo.mark_failed(failed_request, error_message=str(exc))
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

    async def _completar_paises(
        self,
        analysis_request: AnalysisRequest,
        *,
        entity_key: uuid.UUID,
        countries: list[str],
        period_key: str,
        demand_ceiling: float | None,
    ) -> dict[str, str]:
        """Insiste até todo país pedido ter indicador, ou até a causa ser
        definitiva. Devolve `{país: motivo}` só dos DEFINITIVOS.

        AS DUAS CAUSAS, e como são distinguidas sem contrato novo nos quatro
        coletores. Depois de uma passada de coleta + recálculo, um país que
        continua sem indicador está num de dois estados:

          TRANSITÓRIO   alguma task de coleta FALHOU. A fonte não respondeu —
                        503, timeout, erro de gateway. O dado provavelmente
                        existe e a próxima tentativa o traz. Foi o caso do UN
                        Data em 25/08: 78 coletas encerradas em 422 depois de
                        236 respostas 503, e o mesmo dado veio inteiro 36
                        segundos depois de a API voltar.
          DEFINITIVO    nenhuma task falhou e o indicador continua faltando. A
                        coleta rodou, a fonte respondeu, e não tem esse dado
                        para este país. Insistir não muda nada — é a exceção
                        que a decisão D7 abriu.

        A distinção é observável de dentro do Core porque `ensure_coverage`
        agora devolve o resultado das tasks. Não precisa que os coletores
        passem a separar `no_data` de `unavailable` na resposta deles, o que
        seria mudança nos quatro repositórios ao mesmo tempo.

        Enquanto houver transitório e orçamento, dorme e tenta de novo. Quando
        o orçamento acaba, LEVANTA — quem chama marca a análise como `failed`
        com motivo, nunca como `done` incompleta (decisões D3 e D5).
        """
        deadline = utc_now() + timedelta(seconds=settings.analysis_retry_budget_seconds)
        analysis_request.deadline_at = deadline
        await self._session.commit()

        definitivos: dict[str, str] = {}
        tentativa = 0
        while True:
            incompletos = await self._mos_engine.find_incomplete_countries(
                entity_key=entity_key, countries=countries, period_key=period_key
            )
            pendentes = {pais: motivo for pais, motivo in incompletos.items() if pais not in definitivos}
            if not pendentes:
                await self._anotar_progresso(analysis_request, None)
                return definitivos

            tentativa += 1
            transitorios: list[str] = []
            for pais, _motivo in pendentes.items():
                resultados = await self.ensure_coverage(country_iso3=pais, entity_key=entity_key)
                await self._indicator_engine.compute_all(
                    country_iso3=pais,
                    entity_key=entity_key,
                    period_key=period_key,
                    demand_ceiling=demand_ceiling,
                )
                ainda = await self._mos_engine.find_incomplete_countries(
                    entity_key=entity_key, countries=[pais], period_key=period_key
                )
                if not ainda:
                    continue
                falhas = [r for r in resultados if r.status == JobTaskStatus.FAILED]
                if falhas:
                    transitorios.append(pais)
                    fontes = sorted({r.source.value for r in falhas})
                    logger.warning(
                        f"{pais} sem indicador e com coleta falhada em {fontes} (tentativa {tentativa}); "
                        f"causa transitória, vai insistir"
                    )
                else:
                    definitivos[pais] = ainda[pais]
                    logger.warning(
                        f"{pais} sem indicador com a coleta bem-sucedida: a fonte não tem este dado. {ainda[pais]}"
                    )

            if not transitorios:
                await self._anotar_progresso(analysis_request, None)
                return definitivos

            restante = (deadline - utc_now()).total_seconds()
            if restante <= 0:
                raise MarketMindCoreError(
                    f"Análise incompleta depois de {tentativa} tentativas em "
                    f"{settings.analysis_retry_budget_seconds // 3600}h: {', '.join(sorted(transitorios))} seguem "
                    f"sem dado por falha de fonte. Nada parcial é entregue (D3); refaça quando a fonte voltar."
                )

            espera = min(30 * 2 ** (tentativa - 1), settings.analysis_retry_max_backoff_seconds)
            espera = min(espera, restante)
            await self._anotar_progresso(
                analysis_request,
                f"aguardando dado de {', '.join(sorted(transitorios))} — tentativa {tentativa}, "
                f"próxima em {espera:.0f}s (fonte indisponível; a análise não é entregue pela metade)",
            )
            await asyncio.sleep(espera)

    async def _bater(self, analysis_request: AnalysisRequest) -> None:
        """Marca que a análise ainda está viva. Ver analysis_resume_service."""
        analysis_request.heartbeat_at = utc_now()
        await self._session.commit()

    async def _anotar_progresso(self, analysis_request: AnalysisRequest, mensagem: str | None) -> None:
        """Grava no que a análise está esperando. Uma análise legítima
        insistindo numa fonte fora do ar pode levar horas, e sem esta frase ela
        é indistinguível de uma travada — para o usuário e para quem retomar."""
        analysis_request.progress = mensagem
        analysis_request.heartbeat_at = utc_now()
        await self._session.commit()
        if mensagem:
            logger.info(f"AnalysisRequest {analysis_request.id}: {mensagem}")

    async def ensure_coverage(self, *, country_iso3: str, entity_key: uuid.UUID | None) -> list[JobTaskResult]:
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
