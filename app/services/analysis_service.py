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
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import logger
from app.db.models.analysis_request import AnalysisRequest
from app.db.models.mos_result import MosResult
from app.domain.enums import DataSource
from app.repositories.analysis_repository import AnalysisRepository
from app.repositories.country_repository import CountryRepository
from app.repositories.indicator_repository import IndicatorRepository
from app.repositories.mos_repository import MosRepository
from app.repositories.product_repository import ProductRepository
from app.schemas.analyze import AnalyzeRequest
from app.schemas.coverage import CoverageGap, JobTaskResult
from app.services.catalog_service import CatalogService, _merge_localized_markets
from app.services.coverage_service import CoverageService
from app.services.demand_inputs import DemandInputs, demand_inputs
from app.services.enrichment_client import EnrichmentClient, EnrichmentError
from app.services.explanation_client import ExplanationClient
from app.services.indicator_engine import IndicatorEngine
from app.services.indicators import pytrends as pytrends_indicators
from app.services.macro_coverage import fontes_sem_cobertura_de, macro_gaps
from app.services.mos_engine import MosEngine
from app.services.product_identification_service import _fallback_market, _has_terms, _market_to_dict
from app.services.reference_market_client import ReferenceMarketClient, ReferenceMarketError
from app.services.weight_calibration_service import WeightCalibrationService
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


def _top_reference_candidates(candidatos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The bounded shortlist for the DEMAND CEILING.

    Truncates the RAW ranked list, before any requested-country filtering, and
    that ordering is the point. The ceiling has to be query-independent — a
    country must score the same analysed alone or alongside twenty others, which
    is what makes MOS values comparable between analyses at all. Filtering by the
    request first would make the ruler depend on what was asked for.

    Use _champion_candidates for anything that should skip requested countries.
    """
    return list(candidatos or [])[:_MAX_REFERENCE_CANDIDATES]


def _champion_candidates(candidatos: list[dict[str, Any]], requested: list[str]) -> list[dict[str, Any]]:
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
    for cand in candidatos or []:
        iso3 = (cand.get("country") or "").upper()
        if not iso3 or iso3 in requested_set or iso3 in seen:
            continue
        seen.add(iso3)
        out.append(cand)
        if len(out) == _MAX_REFERENCE_CANDIDATES:
            break
    return out


@dataclass(frozen=True)
class CrownedChampion:
    """O mercado de maior MOS do produto entre os pedidos e os candidatos."""

    country: str | None
    mos: float | None
    requested: bool
    # A linha do campeão quando ele NÃO foi pedido: é ela que o relatório e o
    # pacote de evidências mostram ao lado dos pedidos, como benchmark.
    row: MosResult | None
    from_cache: bool
    demand_ceiling: float | None


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
        # The same secret the orchestrator sends on /internal/*, sent back on the
        # one call that goes the other way: starting a run costs money, and the
        # route that starts one is no more for the browser than /internal is.
        headers = {"X-Internal-Token": settings.internal_api_token} if settings.internal_api_token else {}

        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "POST", f"{settings.orchestrator_api_base_url}/run-analysis", json=payload, headers=headers
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
            if current is not None:
                await self._analysis_repo.mark_failed(
                    current, error_message=f"The orchestrator could not be reached: {exc}"
                )
                await self._session.commit()

    async def register(self, request: AnalyzeRequest) -> AnalysisRequest:
        """Registra uma análise por inteiro: resolve o produto com tudo o que a
        identificação trouxe, confere as edições do usuário e cria o registro
        com a marca e o fato do catálogo.

        Eram duas portas e nenhuma completa. A do orquestrador passava por
        /catalog/resolve, que descartava o vocabulário curado e os candidatos de
        referência, e nunca conferia as edições; a de POST /analyze fazia as
        duas coisas e criava o registro sem a marca e sem como o catálogo chegou
        na entidade — uma análise de marca virava análise do produto.
        """
        catalog_service = CatalogService(self._session)
        # A marca é um vínculo de quem pergunta, não parte da identidade. Ver
        # catalog_service.resolve.
        resolved = await catalog_service.resolve(
            request.product_name,
            source="analyze",
            brand=request.brand,
            category=request.category,
            localized_markets=request.localized_markets,
            reference_candidates=request.reference_candidates,
            target_age_min=request.target_age_min,
            target_age_max=request.target_age_max,
            estimated_price=request.estimated_price,
            price_currency=request.price_currency,
        )
        # O que o usuário mudou na tela de revisão só entra no catálogo — que é
        # compartilhado — se fizer sentido para este produto.
        await catalog_service.revisar_edicoes(
            resolved.entity_key,
            category=request.category,
            target_age_min=request.target_age_min,
            target_age_max=request.target_age_max,
            estimated_price=request.estimated_price,
            price_currency=request.price_currency,
        )
        analysis_request = await self._analysis_repo.create(
            entity_key=resolved.entity_key,
            canonical_title=resolved.canonical_title,
            countries=request.countries,
            estimated_price=request.estimated_price,
            price_currency=request.price_currency,
            user_prompt=request.user_prompt,
            # Como o catálogo chegou nesta entidade, e o que foi perguntado: sem
            # o par, uma resolução não é auditável, e uma retomada nunca a refaz.
            catalog_method=resolved.method,
            catalog_match_score=resolved.match_score,
            catalog_match_reasoning=resolved.match_reasoning,
            catalog_query_text=request.product_name,
            # Nulo é "foi do produto" — é o que separa as duas análises depois.
            brand_key=resolved.brand_key,
            brand_name=resolved.brand,
            language=request.language,
        )
        await self._session.commit()
        return analysis_request

    async def vocabulario(self, entity: Any, marca: Any | None) -> dict[str, dict[str, Any]]:
        """Os termos de busca desta análise: do produto, ou DESTA marca.

        O único ponto do pipeline que decide isso. Sem marca são os do produto;
        com marca, os dela — porque "juan valdez café" e "café" são buscas
        diferentes no Google Ads e nunca podem dar o mesmo número.
        """
        # Sem marca a resposta está na própria entidade, e ler dali em vez de
        # ir ao repositório não é atalho: é o que mantém o caminho comum — a
        # análise de produto — sem uma consulta que não tem o que buscar.
        if marca is None:
            return dict(getattr(entity, "localized_markets", None) or {})
        return await self._product_repo.markets_for(entity, marca.brand_key)

    async def candidatos_de_referencia(self, entity: Any, marca: Any | None) -> list[dict[str, Any]]:
        """Os mercados de referência desta análise: do produto, ou DESTA marca.

        Sem marca a resposta está na própria entidade — mesma razão de
        `vocabulario`: o caminho comum não precisa de uma consulta que não tem o
        que buscar.
        """
        if marca is None:
            return list(getattr(entity, "reference_candidates", None) or [])
        return await self._product_repo.reference_candidates_for(entity, marca.brand_key)

    async def _gravar_candidatos(self, entity: Any, marca: Any | None, candidatos: list[dict[str, Any]]) -> None:
        if marca is None:
            entity.reference_candidates = candidatos
            return
        await self._product_repo.save_reference_candidates(entity, marca.brand_key, candidatos)

    async def _gravar_vocabulario(self, entity: Any, marca: Any | None, mercados: dict[str, dict[str, Any]]) -> None:
        if marca is not None:
            await self._product_repo.save_brand_markets(entity.entity_key, marca.brand_key, mercados)
            return
        entity.localized_markets = _merge_localized_markets(entity.localized_markets, mercados)
        # Only countries that had no terms reach this point, so it never changes
        # a fixed one; it does start the semester for a product whose first
        # vocabulary was written here rather than at identification.
        if getattr(entity, "vocabulary_at", None) is None:
            entity.vocabulary_at = utc_now()

    async def ensure_product_enrichment(
        self, entity: Any, countries: list[str], *, marca: Any | None = None, com_candidatos: bool = True
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
        nome_marca = marca.name if marca is not None else None

        mercados = await self.vocabulario(entity, marca)
        missing = [c for c in countries if not _has_terms(mercados.get(c))]
        if missing:
            try:
                # Sem dica de idioma. Pedir que o modelo DIGA o idioma e ao
                # mesmo tempo dizer a ele qual é fecha um círculo: a resposta
                # passa a ser o que mandamos, e a tabela que mandávamos é
                # justamente a que deixou de existir.
                #
                # A MARCA vai junto quando existe, e é ela que muda o que o
                # modelo escreve: "juan valdez café" em vez de "café". Sem isso
                # a análise de marca mediria a categoria.
                fill = [(iso3, None) for iso3 in missing]
                markets = await self._enrichment_client.localized_markets(
                    product_name=product_name,
                    brand=nome_marca,
                    category=entity.category,
                    countries=fill,
                    defining_attributes=entity.defining_attributes,
                    antagonist_products=entity.antagonist_products,
                )
                incoming = {
                    m.country: _market_to_dict(m)
                    for m in markets
                    if m.country in set(missing) and _has_terms(_market_to_dict(m))
                }
                if incoming:
                    await self._gravar_vocabulario(entity, marca, incoming)
                    mercados.update(incoming)
            except EnrichmentError as exc:
                logger.warning(f"Background localized_markets enrichment failed, using deterministic fallback: {exc}")
            # Deterministic fallback so coverage/compute never strand a country.
            still_missing = [c for c in countries if not _has_terms(mercados.get(c))]
            if still_missing:
                reserva = {iso3: _fallback_market(iso3, product_name, nome_marca) for iso3 in still_missing}
                await self._gravar_vocabulario(entity, marca, reserva)
                mercados.update(reserva)

        candidatos_atuais = await self.candidatos_de_referencia(entity, marca)
        if com_candidatos and not candidatos_atuais:
            try:
                supported = [c.iso3 for c in await self._country_repo.list_all()]
                supported_set = set(supported)
                candidates: list[dict[str, Any]] = []
                seen: set[str] = set()
                # A marca vai junto, como no vocabulário: onde o café é mais
                # procurado não é onde a Juan Valdez é mais procurada, e os
                # termos de cada candidato têm de nomear a marca para medirem a
                # demanda DELA.
                for cand in await self._reference_market_client.candidates(
                    product_name=product_name,
                    brand=nome_marca,
                    category=entity.category,
                    supported_countries=supported,
                ):
                    entry = _market_to_dict(cand)
                    if cand.country in supported_set and cand.country not in seen and _has_terms(entry):
                        candidates.append({"country": cand.country, **entry})
                        seen.add(cand.country)
                if candidates:
                    await self._gravar_candidatos(entity, marca, candidates)
            except ReferenceMarketError as exc:
                logger.warning(f"Background reference-market candidates unavailable: {exc}")

    async def _insumos(
        self, entity: Any, iso3: str, marca: Any | None, *, candidate_market: dict[str, Any] | None = None
    ) -> DemandInputs | None:
        """Os insumos de demanda deste país, pelo construtor único (ver
        app/services/demand_inputs.py). None para país fora do cadastro."""
        country = await self._country_repo.get_by_iso3(iso3)
        if country is None:
            return None
        return await demand_inputs(
            self._product_repo,
            entity,
            country,
            brand_key=marca.brand_key if marca is not None else None,
            candidate_market=candidate_market,
        )

    async def _demand_gaps(
        self, insumos: DemandInputs, entity_key: uuid.UUID, period_key: str
    ) -> tuple[list[CoverageGap], int, bool]:
        """(gaps, termos já guardados, relacionadas frescas?) da demanda de um
        país — pedido ou candidato, pelas mesmas perguntas.

        A demanda não passa por `check_coverage`: a pergunta de frescor dela é
        feita aqui, por TERMO, que é a granularidade que a cobertura não sabe
        expressar. Para os candidatos ela já foi feita de outro jeito — o gap
        nascia com a lista inteira e a decisão ficava com `check_coverage`, que
        para pytrends responde "vencido" sempre, então TODA análise rebaixava a
        demanda inteira dos candidatos, idêntica à da anterior, pagando por
        chamada. Medido em 23/09/2026.
        """
        gaps: list[CoverageGap] = []
        if not insumos.search_terms or insumos.location is None:
            return gaps, 0, False
        # Term-incremental demand ("só baixa se um termo novo aparece"): collect
        # ONLY the search terms that don't yet have volume data — already-cached
        # terms are never re-downloaded. Os termos da categoria viajam na mesma
        # coleta (decisão D4), sem request nova.
        termos_para_volume = insumos.volume_terms
        missing_sv = await pytrends_indicators.missing_volume_terms(termos_para_volume, insumos.location)
        reaproveitados = len(termos_para_volume) - len(missing_sv)
        if missing_sv:
            gaps.append(
                CoverageGap(
                    source=DataSource.PYTRENDS,
                    entity_key=entity_key,
                    country=insumos.country,
                    indicator_code="search_volume",
                    period_key=period_key,
                    collection_params={
                        "location": insumos.location,
                        "language": insumos.language,
                        "keywords": missing_sv,
                    },
                    force=True,
                )
            )
        # Related keywords (intent) are a function of the seed SET, not of
        # individual terms, so re-collect only when the current seed's related
        # rows are absent or past their age (`related_rows_exist` já considera
        # idade) — i.e. a genuinely new term entered the top-20 seeds.
        relacionadas_frescas = bool(insumos.seed_terms) and await pytrends_indicators.related_rows_exist(
            insumos.seed_terms, insumos.location
        )
        if insumos.seed_terms and not relacionadas_frescas:
            gaps.append(
                CoverageGap(
                    source=DataSource.PYTRENDS,
                    entity_key=entity_key,
                    country=insumos.country,
                    indicator_code="keywords_for_keywords",
                    period_key=period_key,
                    collection_params={
                        "location": insumos.location,
                        "language": insumos.language,
                        "seed_keywords": insumos.seed_terms,
                    },
                    # force: we only reach here when the CURRENT seed has no
                    # related rows, so it MUST collect regardless of a stale
                    # "covered" coverage_index row from a prior seed set —
                    # otherwise search_intent_score is stranded → abort.
                    force=True,
                )
            )
        return gaps, reaproveitados, relacionadas_frescas

    async def ensure_coverage(
        self,
        *,
        country_iso3: str,
        entity_key: uuid.UUID | None,
        period_key: str,
        marca: Any | None = None,
        terms_used: dict[str, Any] | None = None,
        reuse_report: dict[str, Any] | None = None,
    ) -> list[JobTaskResult]:
        """Checks (read-only) every gap this country/product needs, and only
        dispatches collection (check_or_collect) for the ones actually missing.

        Devolve o resultado das tasks despachadas — vazio quando nada precisou
        ser coletado. É esse resultado que distingue as duas causas de um país
        continuar sem indicador (ver `_completar_paises`): task FALHADA é fonte
        fora do ar, e vale insistir; coleta que terminou sem falha e mesmo assim
        não produziu o indicador é fonte que não tem aquele dado.

        `reuse_report`, quando dado, recebe o OUTRO lado da conta: quantos gaps
        de cada fonte já estavam frescos e por isso não viraram tarefa nenhuma.
        Sem ele, uma fonte inteiramente aproveitada some do relato — e "não
        aparece" é indistinguível de "não foi consultada", que é exatamente a
        pergunta que o relato existe para responder."""
        gaps: list[CoverageGap] = []
        demanda_reaproveitada = 0
        relacionadas_frescas = False

        if entity_key is not None:
            entity = await self._product_repo.get_by_entity_key(entity_key)
            # Demand is collected from Google Ads keyword data (2026-07-15) with
            # the country's OWN terms, read by the SAME builder the indicator
            # engine reads with: rótulos de sementes diferentes deixariam o
            # intent órfão.
            insumos = await self._insumos(entity, country_iso3, marca) if entity is not None else None
            # What this country is actually searched for, recorded for the
            # caller. It is the one part of the pipeline a model wrote, and
            # nothing downstream reveals it: a wrong native term produces a
            # perfectly formed analysis of the wrong words.
            if terms_used is not None and insumos is not None:
                terms_used.update(
                    {
                        "country": country_iso3,
                        "location": insumos.location,
                        "language": insumos.language,
                        "search_terms": list(insumos.search_terms),
                        "category_terms": list(insumos.category_terms),
                    }
                )
            if insumos is not None:
                gaps, demanda_reaproveitada, relacionadas_frescas = await self._demand_gaps(
                    insumos, entity_key, period_key
                )

        # Os gaps macro vêm do construtor ÚNICO (app/services/macro_coverage.py):
        # quem coleta durante a análise e quem pré-aquece fora dela precisam
        # produzir exatamente a mesma lista, senão o pré-aquecimento esquenta
        # uma coisa e a análise pede outra.
        pais_macro = await self._country_repo.get_by_iso3(country_iso3)
        gaps.extend(macro_gaps(country_iso3, fontes_sem_cobertura=fontes_sem_cobertura_de(pais_macro)))

        if not gaps:
            if reuse_report is not None:
                reuse_report["reused"] = {"pytrends": demanda_reaproveitada} if demanda_reaproveitada else {}
                reuse_report["related_fresh"] = relacionadas_frescas
                reuse_report["not_covering"] = sorted(f.value for f in fontes_sem_cobertura_de(pais_macro))
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
        if reuse_report is not None:
            reaproveitados: dict[str, int] = {}
            for gap, status in zip(unique_gaps, statuses, strict=True):
                if status.covered and not gap.force:
                    reaproveitados[gap.source.value] = reaproveitados.get(gap.source.value, 0) + 1
            if demanda_reaproveitada or relacionadas_frescas:
                reaproveitados["pytrends"] = reaproveitados.get("pytrends", 0) + demanda_reaproveitada
            reuse_report["reused"] = reaproveitados
            reuse_report["related_fresh"] = relacionadas_frescas
            # Uma fonte que não cobre este país não é o mesmo que uma fonte que
            # não foi pedida: é uma resposta, e a única honesta. Ver
            # `fontes_sem_cobertura_de` — a ausência do código do país naquela
            # fonte É o dado.
            reuse_report["not_covering"] = sorted(f.value for f in fontes_sem_cobertura_de(pais_macro))
        if not missing:
            return []
        _job_id, resultados = await self._coverage_service.check_or_collect(missing, requested_by="analysis_service")
        return resultados

    async def ensure_candidates_coverage(
        self,
        entity: Any,
        requested: list[str],
        *,
        period_key: str,
        marca: Any | None = None,
        reuse_report: dict[str, Any] | None = None,
    ) -> list[JobTaskResult]:
        """Collect each reference-market candidate's FULL demand — search_volume
        AND keywords_for_keywords — exactly like a requested country, so its
        volume is measured through the identical pipeline (including the related-
        keyword expansion) and the ruler stays comparable. Collected regardless
        of the requested set (so the ruler is query-independent) and persisted /
        reused via the coverage cache. Candidates that ARE requested countries
        are skipped here — already collected under their own localized terms
        (pulled once).

        Devolve as tarefas despachadas, e preenche `reuse_report` com o que já
        estava guardado. Antes não devolvia nada, e por isso o passo MAIS CARO
        do sistema não aparecia em relato nenhum: três análises seguidas de um
        produto levaram 594s, 203s e 396s, e quase todo o custo repetido era
        trabalho de candidato refeito."""
        gaps: list[CoverageGap] = []
        demanda_reaproveitada = 0
        relacionadas_frescas = 0
        candidatos = await self.candidatos_de_referencia(entity, marca)
        for cand in _champion_candidates(candidatos, requested):
            iso3 = cand.get("country")
            if not iso3 or iso3 in requested:
                continue
            insumos = await self._insumos(entity, iso3, marca, candidate_market=cand)
            if insumos is None:
                continue
            gaps_do_pais, reaproveitados, frescas = await self._demand_gaps(insumos, entity.entity_key, period_key)
            gaps.extend(gaps_do_pais)
            demanda_reaproveitada += reaproveitados
            relacionadas_frescas += 1 if frescas else 0
        if reuse_report is not None:
            reuse_report["reused"] = {"pytrends": demanda_reaproveitada} if demanda_reaproveitada else {}
            reuse_report["related_fresh"] = relacionadas_frescas
        if not gaps:
            return []
        # Sem `check_coverage` aqui: todo gap que chegou a esta lista já é, por
        # construção, algo que falta ou venceu — as duas perguntas foram feitas
        # em `_demand_gaps`, por termo. Perguntar de novo seria a mesma pergunta
        # duas vezes e uma chamada HTTP a mais por análise.
        _job_id, resultados = await self._coverage_service.check_or_collect(
            gaps, requested_by="analysis_service:reference_candidates"
        )
        return resultados

    async def resolve_demand_ceiling(
        self, entity: Any, countries: list[str], period_key: str, *, marca: Any | None = None
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
        - the requested countries are measured too, and one above every
          candidate joins the candidates (at the top, persisted), so the ruler
          is always the largest market measured.
        - fixed — None (no candidate had volume), so the engine uses the 2M bound.
        Volumes are read from already-collected data via compute_trend_score."""
        ceiling, ruler, novo = await self.measure_demand_ceiling(entity, countries, period_key, marca=marca)
        # A régua é o MAIOR mercado medido, e um país pedido pode ser maior que
        # todos os candidatos — medido em 26/10/2026: o Brasil com 17.020
        # buscas contra os EUA com 15.800; o Brasil batia no teto e a narrativa
        # dizia que os EUA eram o maior mercado avaliado. Esse país ENTRA nos
        # candidatos do produto, no topo, e fica: a régua sobe agora e é a
        # mesma nas próximas análises, peçam ou não esse país, o que mantém a
        # nota de cada país independente do pedido. O campeão guardado com a
        # régua antiga deixa de valer sozinho: ele carrega a régua com que foi
        # calculado (ver ProductRepository.champion_for).
        if novo is not None:
            candidatos = await self.candidatos_de_referencia(entity, marca)
            await self._gravar_candidatos(entity, marca, [novo, *candidatos])
            await self._session.commit()
            logger.info(f"Régua de demanda subiu para {ruler['country']} ({ruler['volume']:.0f} buscas/mês)")
        return ceiling, ruler

    async def measure_demand_ceiling(
        self, entity: Any, countries: list[str], period_key: str, *, marca: Any | None = None
    ) -> tuple[float | None, dict[str, Any], dict[str, Any] | None]:
        """(teto, régua, candidato novo) — a medição de `resolve_demand_ceiling`
        sem gravar nada. O candidato novo é o país pedido que passou de todos
        os candidatos, com o vocabulário que o mediu, ou None.

        Separada porque há quem precise saber a régua ANTES de decidir coletar
        (o campeão guardado só vale para a régua com que foi calculado), e
        gravar um país pedido no topo dos candidatos antes de os candidatos
        serem coletados o colocaria lá só por ser o único com dado."""
        candidatos = await self.candidatos_de_referencia(entity, marca)
        # Os mercados medidos: os candidatos de referência e os países pedidos,
        # cada um pelo vocabulário que foi de fato coletado — o desta análise
        # (da marca, quando há marca) se o país foi pedido, o do candidato se
        # não foi. Medir um país pedido com os termos do candidato daria dois
        # números para a mesma coisa.
        mercados: dict[str, dict[str, Any] | None] = {}
        for cand in _top_reference_candidates(candidatos):
            iso3 = cand.get("country")
            if iso3:
                mercados[iso3] = None if iso3 in countries else cand
        for iso3 in countries:
            mercados.setdefault(iso3, None)

        measured: list[dict[str, Any]] = []
        for iso3, candidato in mercados.items():
            insumos = await self._insumos(entity, iso3, marca, candidate_market=candidato)
            volume = await self._volume_de_busca(insumos, period_key) if insumos is not None else None
            if volume:
                measured.append({"country": iso3, "volume": volume})
        if not measured:
            return None, {"mode": "fixed"}, None
        winner = max(measured, key=lambda m: m["volume"])
        ruler = {"mode": "reference", "country": winner["country"], "volume": winner["volume"], "candidates": measured}

        novo = None
        if winner["country"] not in {c.get("country") for c in candidatos}:
            vocabulario = await self.vocabulario(entity, marca)
            novo = {"country": winner["country"], **(vocabulario.get(winner["country"]) or {})}
        return winner["volume"], ruler, novo

    async def _volume_de_busca(self, insumos: DemandInputs, period_key: str) -> float | None:
        """O volume mensal de busca de um país, lido do que já foi coletado,
        com os MESMOS insumos da nota — sementes com a categoria, vetos da
        curadoria e de outras marcas —, para que a régua compare igual com
        igual."""
        if not (insumos.search_terms and insumos.location):
            return None
        r = await pytrends_indicators.compute_trend_score(
            insumos.search_terms,
            insumos.location,
            period_key,
            seed_terms=insumos.seed_terms,
            language=insumos.language,
            outras_marcas=insumos.outras_marcas,
            excluded_terms=insumos.excluded_terms,
        )
        return float(r.raw_value) if r.raw_value is not None and float(r.raw_value) > 0 else None

    async def score_reference_candidates(
        self,
        *,
        entity: Any,
        requested: list[str],
        period_key: str,
        demand_ceiling: float | None,
        marca: Any | None = None,
    ) -> list[MosResult]:
        """Best-effort MOS for the product's reference-candidate markets the user
        did NOT request, so the best-MOS market (the "champion") can be surfaced
        for benchmark even when it wasn't selected. MOS uses fixed 100/0 TOPSIS
        anchors → query-independent, so this never changes the requested scores.
        The candidates' demand is already collected (the ruler); only free World
        Bank/OECD/UN data + compute remain, so ~zero paid cost. A candidate that
        can't be fully scored is simply skipped (never aborts the analysis).

        Com a marca da análise em tudo: sem ela os candidatos de uma marca eram
        gravados nas linhas do PRODUTO e sobrescreviam o MOS dele."""
        entity_key = entity.entity_key
        if entity_key is None:
            return []
        brand_key = marca.brand_key if marca is not None else None
        requested_set = set(requested)
        attempted: list[str] = []
        seen: set[str] = set()
        candidatos = await self.candidatos_de_referencia(entity, marca)
        for cand in _champion_candidates(candidatos, requested):
            iso3 = cand.get("country")
            if not iso3 or iso3 in requested_set or iso3 in seen:
                continue
            seen.add(iso3)
            country = await self._country_repo.get_by_iso3(iso3)
            if country is None or country.iso2 is None:
                continue
            try:
                await self.ensure_coverage(country_iso3=iso3, entity_key=None, period_key=period_key)
                await self._indicator_engine.compute_all(
                    country_iso3=iso3,
                    entity_key=entity_key,
                    period_key=period_key,
                    demand_ceiling=demand_ceiling,
                    candidate_market=cand,
                    brand_key=brand_key,
                )
                attempted.append(iso3)
            except Exception as exc:  # noqa: BLE001 — benchmark is best-effort, never fails the analysis
                logger.warning(f"Reference candidate {iso3} could not be scored (skipped): {exc}")
        if not attempted:
            return []
        return await self._mos_engine.compute_mos(
            entity_key=entity_key, countries=attempted, period_key=period_key, brand_key=brand_key
        )

    async def crown_champion(
        self,
        *,
        entity: Any,
        requested: list[str],
        requested_results: list[dict[str, Any]],
        period_key: str,
        marca: Any | None = None,
    ) -> CrownedChampion:
        """Coroa o mercado de maior MOS do produto — pedido ou candidato — e o
        guarda para o período, com a régua de demanda de agora.

        A decisão era do orquestrador, em três chamadas: lia o campeão guardado
        na coleta dos candidatos, a régua subia na chamada seguinte e apagava o
        campeão, e o orquestrador coroava o que já tinha lido, medido contra a
        régua de antes. Agora quem decide é quem tem a régua, numa chamada só,
        e o guardado só vale quando foi calculado contra a régua de agora.

        Empate fica com o mercado pedido: a coroa diz "o melhor mercado deste
        produto", e entre dois iguais o que o comprador escolheu é a resposta
        que ele entende.
        """
        brand_key = marca.brand_key if marca is not None else None
        # Lida, não gravada: subir a régua é do passo da régua, que roda antes
        # de os países pedidos serem medidos. Aqui ela só precisa ser a mesma.
        ceiling, _ruler, _novo = await self.measure_demand_ceiling(entity, requested, period_key, marca=marca)
        pedidos = set(requested)
        disputa: list[tuple[str, float]] = [
            (str(r["country"]), float(r["mos"]))
            for r in requested_results
            if r.get("mos") is not None and r.get("country") in pedidos
        ]

        cached, pais, mos = await self._product_repo.champion_for(
            entity, brand_key, period_key, demand_ceiling=ceiling
        )
        linhas: dict[str, MosResult] = {}
        if cached:
            # Pedido, o número que vale é o desta análise; fora do pedido, o
            # guardado, que é o mesmo MOS de âncoras fixas medido contra esta
            # mesma régua.
            if pais is not None and mos is not None and pais not in pedidos:
                disputa.append((pais, float(mos)))
        else:
            for row in await self.score_reference_candidates(
                entity=entity, requested=requested, period_key=period_key, demand_ceiling=ceiling, marca=marca
            ):
                if row.mos is not None:
                    disputa.append((row.country, float(row.mos)))
                    linhas[row.country] = row

        # `max` devolve o primeiro dos empatados, e os pedidos vêm primeiro.
        campeao = max(disputa, key=lambda par: par[1], default=None)
        novo = (campeao[0], campeao[1]) if campeao else (None, None)
        guardado = (pais, None if mos is None else float(mos))
        # Gravado sempre que muda, inclusive "nada pontuável" (país nulo): o
        # período é o que marca o trabalho como feito, e sem gravar a mesma
        # coleta infrutífera rodaria em toda análise seguinte. Um pedido que
        # passa do guardado o substitui, ou o guardado volta a ser coroado na
        # próxima análise que não pedir esse país.
        if not cached or novo != guardado:
            await self._product_repo.set_champion(
                entity,
                country=novo[0],
                mos=None if novo[1] is None else Decimal(str(novo[1])),
                period_key=period_key,
                demand_ceiling=ceiling,
                brand_key=brand_key,
            )
            await self._session.commit()

        fora_do_pedido = novo[0] is not None and novo[0] not in pedidos
        row = None
        if fora_do_pedido:
            row = linhas.get(novo[0]) or await self._mos_repo.get_mos_result(
                entity.entity_key, novo[0], "month", period_key, brand_key
            )
        return CrownedChampion(
            country=novo[0],
            mos=novo[1],
            requested=novo[0] is not None and not fora_do_pedido,
            row=row,
            from_cache=cached,
            demand_ceiling=ceiling,
        )

    async def build_evidence(
        self,
        *,
        entity: Any,
        requested: list[str],
        period_key: str,
        language: str | None,
        demand_ruler: dict[str, Any] | None = None,
        marca: Any | None = None,
    ) -> dict[str, Any] | None:
        """O pacote de evidências da análise: os países pedidos e, quando o
        campeão coroado não foi pedido, ele também, com o detalhamento inteiro.

        O campeão é o guardado por `crown_champion` para este período e esta
        régua. O orquestrador montava as marcas `requested`/`is_champion` por
        cima do pacote, que só trazia os pedidos, e a reconstrução na leitura
        saía sem marca nenhuma e sem a régua.

        `demand_ruler` nulo (análises gravadas antes de a régua ser guardada)
        mede a régua do que já está coletado, sem gravar nada.
        """
        brand_key = marca.brand_key if marca is not None else None
        if demand_ruler is None:
            teto, demand_ruler, _novo = await self.measure_demand_ceiling(entity, requested, period_key, marca=marca)
        else:
            teto = demand_ruler.get("volume") if demand_ruler.get("mode") == "reference" else None
        linhas = []
        for country in requested:
            row = await self._mos_repo.get_mos_result(entity.entity_key, country, "month", period_key, brand_key)
            if row is not None:
                linhas.append(row)
        cached, pais, _mos = await self._product_repo.champion_for(entity, brand_key, period_key, demand_ceiling=teto)
        if cached and pais is not None and pais not in requested:
            row = await self._mos_repo.get_mos_result(entity.entity_key, pais, "month", period_key, brand_key)
            if row is not None:
                linhas.append(row)
        if not linhas:
            return None
        return await self._explanation_client.build_evidence(
            entity_key=entity.entity_key,
            mos_results=linhas,
            period_key=period_key,
            demand_ruler=demand_ruler,
            requested=requested,
            language=language,
        )
