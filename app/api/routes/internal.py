"""The tools the orchestrator drives an analysis with.

Each one is a step that used to be a private method inside AnalysisService,
sequenced by a hard-coded loop. They are endpoints now so the agent can call
them, and the sequencing can move without the method moving with it.

Two properties every route here holds to:

IDEMPOTENT. Calling one twice for the same period must not collect twice or
compute twice. That is what makes a repeated analysis cheap, and it is also
what makes a resumed one safe: resuming is re-running.

EXPLICIT PERIOD. No route reads a clock to decide which month it is working on.
An analysis that starts before midnight and finishes after it must not change
period halfway, and a tool that picks its own period cannot be replayed to the
same answer.
"""

import uuid
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_internal_token
from app.core.config import settings
from app.core.logging import logger
from app.db.models.brand import Brand
from app.db.session import get_session
from app.repositories.analysis_repository import AnalysisRepository
from app.repositories.product_repository import ProductRepository
from app.schemas.analyze import AnalysisStatusResponse, AnalyzeRequest
from app.schemas.internal import (
    AnalysisDetailResponse,
    AnalysisStateResponse,
    CalibrateWeightsRequest,
    CalibrateWeightsResponse,
    CandidatesCoverageRequest,
    CandidatesCoverageResponse,
    CompleteAnalysisRequest,
    CrownChampionRequest,
    CrownChampionResponse,
    DemandAbsence,
    DemandAbsenceRequest,
    DemandAbsenceResponse,
    DemandCeilingRequest,
    DemandCeilingResponse,
    EnrichTermsRequest,
    EnrichTermsResponse,
    EnsureCoverageRequest,
    EnsureCoverageResponse,
    EvidenceRequest,
    EvidenceResponse,
    FailAnalysisRequest,
    MarkRunningRequest,
    NarrateRequest,
    NarrateResponse,
    ProgressRequest,
    ProgressResponse,
    RegisterAnalysisRequest,
    RegisteredAnalysisResponse,
    ScoreCandidatesRequest,
    ScoreCandidatesResponse,
    ScoredCandidate,
    SetChampionRequest,
)
from app.schemas.mos import MosResultResponse
from app.services.analysis_service import AnalysisService
from app.services.explanation_client import ExplanationClient
from app.services.presentation import present_rows
from app.services.weight_calibration_service import WeightCalibrationService
from app.utils.language import analysis_language
from app.utils.time import utc_now

router = APIRouter(prefix="/internal", tags=["Internal"], dependencies=[Depends(require_internal_token)])


async def _load(session: AsyncSession, request_id: uuid.UUID):  # noqa: ANN202
    analysis_request = await AnalysisRepository(session).get(request_id)
    if analysis_request is None:
        raise HTTPException(status_code=404, detail=f"Analysis request {request_id} not found")
    return analysis_request


@router.post(
    "/analyses/register",
    response_model=RegisteredAnalysisResponse,
    status_code=201,
    summary="Resolve the product and register an analysis, completely",
    description=(
        "Same body as POST /analyze. Resolves the product with everything the "
        "identification produced (localized_markets and reference_candidates "
        "included), checks the user's edits against the product before they "
        "enter the shared catalog, and creates the record with the brand and "
        "how the catalog matched. The one way an analysis is registered; "
        "POST /analyze uses it too."
    ),
)
async def register_full_analysis(
    request: AnalyzeRequest, session: AsyncSession = Depends(get_session)
) -> RegisteredAnalysisResponse:
    analysis_request = await AnalysisService(session).register(request)
    return RegisteredAnalysisResponse(
        request_id=analysis_request.id,
        status=analysis_request.status,
        entity_key=analysis_request.entity_key,
        canonical_title=analysis_request.canonical_title,
        brand_key=analysis_request.brand_key,
        brand_name=analysis_request.brand_name,
    )


@router.post(
    "/analyses",
    response_model=RegisteredAnalysisResponse,
    status_code=201,
    summary="Register an analysis",
    deprecated=True,
    description=(
        "DEPRECATED — POST /internal/analyses/register resolves and registers "
        "in one call, without dropping the curated vocabulary or the review "
        "of the user's edits.\n\n"
        "Creates the record an analysis is tracked and billed against. The "
        "product must already be resolved — the entity_key comes from "
        "POST /catalog/resolve — because identity decides what is being "
        "analysed, and guessing it here would let two different products share "
        "one record."
    ),
)
async def register_analysis(
    request: RegisterAnalysisRequest, session: AsyncSession = Depends(get_session)
) -> RegisteredAnalysisResponse:
    analysis_request = await AnalysisRepository(session).create(
        entity_key=request.entity_key,
        canonical_title=request.canonical_title,
        countries=request.countries,
        estimated_price=request.estimated_price,
        price_currency=request.price_currency,
        user_prompt=request.user_prompt,
        catalog_method=request.catalog_method,
        catalog_match_score=request.catalog_match_score,
        catalog_match_reasoning=request.catalog_match_reasoning,
        catalog_query_text=request.catalog_query_text,
        brand_key=request.brand_key,
        brand_name=request.brand_name,
        language=request.language,
    )
    await session.commit()
    return RegisteredAnalysisResponse(request_id=analysis_request.id, status=analysis_request.status)


@router.get(
    "/analyses/orphaned",
    summary="Analyses that are running and nobody is running",
    description=(
        "Every analysis in `running` whose execution claim is absent or too old "
        "to belong to a live process.\n\n"
        "An agent that has just started asks this and resumes what it finds. It "
        "knows it restarted; what it could not know is which analyses were "
        "orphaned by that restart, and without this it had to wait for the "
        "fifteen-minute sweep to notice — fifteen minutes of a screen saying "
        "'in progress' with nothing progressing."
    ),
)
async def list_orphaned_analyses(session: AsyncSession = Depends(get_session)) -> list[uuid.UUID]:
    paradas = await AnalysisRepository(session).list_unclaimed_running(
        claim_stale_after_seconds=settings.analysis_claim_stale_after_seconds
    )
    return [a.id for a in paradas]


@router.post(
    "/analyses/{request_id}/claim",
    summary="Take over running this analysis",
    description=(
        "Answers whether the caller may run it. False when another process "
        "holds a live claim — the claim is renewed on every progress report, so "
        "one that has stopped moving belongs to a process that has stopped.\n\n"
        "This is what lets two agents run side by side: the one that loses the "
        "race does not run, instead of both executing the same analysis."
    ),
)
async def claim_analysis(
    request_id: uuid.UUID, runner: str, session: AsyncSession = Depends(get_session)
) -> dict[str, bool]:
    analysis_request = await _load(session, request_id)
    granted = await AnalysisRepository(session).claim(
        analysis_request,
        runner=runner,
        claim_stale_after_seconds=settings.analysis_claim_stale_after_seconds,
    )
    await session.commit()
    return {"granted": granted}


@router.get(
    "/analyses/{request_id}",
    response_model=AnalysisDetailResponse,
    summary="Read an analysis back",
    description=(
        "What a resumed run needs to pick the analysis back up: which product, "
        "which countries, what the buyer asked, and where it got to.\n\n"
        "Resuming is re-running — every step is idempotent — so the only thing "
        "that must not be re-decided is what was already decided."
    ),
)
async def read_analysis(request_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> AnalysisDetailResponse:
    analysis_request = await _load(session, request_id)
    return AnalysisDetailResponse(
        request_id=analysis_request.id,
        status=analysis_request.status,
        entity_key=analysis_request.entity_key,
        canonical_title=analysis_request.canonical_title,
        catalog_method=analysis_request.catalog_method,
        catalog_match_score=analysis_request.catalog_match_score,
        # Calculado AQUI, e não por quem lê, porque o limiar é desta casa. O
        # agente chegou a repetir o 0.4 do lado dele só para rotular, e um
        # número repetido é um número que envelhece sozinho: mudar o limiar
        # aqui deixaria o rótulo de lá mentindo sem nada acusar.
        catalog_match_borderline=_no_limiar(analysis_request.catalog_match_score),
        catalog_match_reasoning=analysis_request.catalog_match_reasoning,
        catalog_query_text=analysis_request.catalog_query_text,
        brand_key=analysis_request.brand_key,
        brand_name=analysis_request.brand_name,
        countries=list(analysis_request.countries or []),
        user_prompt=analysis_request.user_prompt,
        language=analysis_request.language,
        progress=analysis_request.progress,
        explanation=analysis_request.explanation,
        error_message=analysis_request.error_message,
        result=analysis_request.result,
    )


@router.post(
    "/analyses/{request_id}/running",
    response_model=AnalysisStateResponse,
    summary="Mark an analysis as running, with its deadline",
    description=(
        "`deadline_at` (optional body) is how long the run may insist on a "
        "source that is down. Stored the first time only — a resumed run sends "
        "it again and must not push the end forward. The resume sweep and the "
        "sentinel fail an analysis that is still stuck past it. An analysis that "
        "already ended stays as it is and its status comes back."
    ),
)
async def mark_running(
    request_id: uuid.UUID, request: MarkRunningRequest | None = None, session: AsyncSession = Depends(get_session)
) -> AnalysisStateResponse:
    analysis_request = await _load(session, request_id)
    repo = AnalysisRepository(session)
    if await repo.mark_running(analysis_request) and request is not None and request.deadline_at is not None:
        await repo.set_deadline(analysis_request, request.deadline_at)
    analysis_request.heartbeat_at = utc_now()
    await session.commit()
    return AnalysisStateResponse(request_id=analysis_request.id, status=analysis_request.status)


@router.get(
    "/analyses/{request_id}/report",
    response_model=AnalysisStatusResponse,
    summary="The whole answer, shaped for a reader",
    description=(
        "Everything a report needs in one call: the scored markets, the "
        "narrative, the full evidence packet, and the brand/category split of "
        "the product's identity.\n\n"
        "Distinct from GET /analyses/{request_id}, which is what a RESUMING RUN "
        "needs — what was already decided, and nothing that has to be assembled. "
        "This one assembles: it rebuilds the evidence packet for analyses stored "
        "before that column existed and reads the identity back off the entity. "
        "Two callers, two shapes, one row."
    ),
)
async def read_report(request_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> AnalysisStatusResponse:
    # Imported here rather than at module load: app.api.routes.analyze imports
    # the analysis service, and that imports back into this module's world.
    from app.api.routes.analyze import _identidade, _load_breakdown, _status_payload

    analysis_request = await _load(session, request_id)
    breakdown = await _load_breakdown(session, analysis_request)
    brand, category_title = await _identidade(session, analysis_request)
    return _status_payload(analysis_request, breakdown, brand, category_title)


@router.patch(
    "/analyses/{request_id}/progress",
    response_model=ProgressResponse,
    summary="Say what the analysis is waiting on, and that it is alive",
    description=(
        "Records what the analysis is currently waiting for, and stamps the "
        "heartbeat.\n\n"
        "Both matter and they are different. The note is for whoever is "
        "watching: an analysis legitimately insisting on a source that is down "
        "can take hours, and without a sentence saying so it is "
        "indistinguishable from one that is stuck. The heartbeat is for "
        "whoever resumes: it is how a run that died with its process is told "
        "apart from one that is simply slow."
    ),
)
async def record_progress(
    request_id: uuid.UUID, request: ProgressRequest, session: AsyncSession = Depends(get_session)
) -> ProgressResponse:
    analysis_request = await _load(session, request_id)
    analysis_request.progress = request.message
    analysis_request.heartbeat_at = utc_now()
    # A reivindicação é renovada aqui e em nenhum outro lugar. É o que a torna
    # um sinal de vida: quem está trabalhando reporta progresso, quem morreu
    # para de reportar, e a reivindicação envelhece sozinha.
    if analysis_request.claimed_by is not None:
        analysis_request.claimed_at = analysis_request.heartbeat_at
    await session.commit()
    if request.message:
        logger.info(f"AnalysisRequest {request_id}: {request.message}")
    return ProgressResponse(request_id=request_id, progress=analysis_request.progress)


@router.post(
    "/analyses/{request_id}/complete",
    response_model=AnalysisStateResponse,
    summary="Complete an analysis with its scored markets",
    description=(
        "Marks the request done. `result` must carry at least one scored "
        "market: completing with nothing is how a buyer ends up billed for an "
        "empty answer, and billing is tied to this state.\n\n"
        "`explanation` is optional here on purpose. The numbers are the answer "
        "and the prose is a reading of them, so the request completes with its "
        "scores and the narrative is attached afterwards — a single model call "
        "once took longer than the entire rest of the analysis.\n\n"
        "409 when the analysis already failed. Completing a done one again is "
        "allowed: resuming is re-running."
    ),
)
async def complete_analysis(
    request_id: uuid.UUID, request: CompleteAnalysisRequest, session: AsyncSession = Depends(get_session)
) -> AnalysisStateResponse:
    analysis_request = await _load(session, request_id)
    concluida = await AnalysisRepository(session).mark_done(
        analysis_request,
        # As marcas e o que se mostra de cada linha são decididos aqui, não por
        # quem entrega (ver app/services/presentation.py).
        result=present_rows(
            request.result,
            requested=list(analysis_request.countries or []),
            language=analysis_language(analysis_request.language),
        ),
        explanation=request.explanation,
        breakdown=request.breakdown,
    )
    if not concluida:
        # Já falhou: o comprador leu o motivo, e concluir agora seria cobrar
        # por uma análise que a tela dele diz que não houve.
        raise HTTPException(status_code=409, detail=f"Analysis request {request_id} already failed")
    await session.commit()
    return AnalysisStateResponse(request_id=request_id, status=analysis_request.status)


@router.post(
    "/analyses/{request_id}/narrative",
    response_model=AnalysisStateResponse,
    summary="Attach the narrative to an already completed analysis",
    description=(
        "Separate from completing on purpose: the answer lands first and the "
        "prose follows. A failure here must never undo an analysis that is "
        "already delivered."
    ),
)
async def attach_narrative(
    request_id: uuid.UUID, request: NarrateResponse, session: AsyncSession = Depends(get_session)
) -> AnalysisStateResponse:
    analysis_request = await _load(session, request_id)
    await AnalysisRepository(session).set_explanation(analysis_request, request.narrative)
    await session.commit()
    return AnalysisStateResponse(request_id=request_id, status=analysis_request.status)


@router.post(
    "/analyses/{request_id}/fail",
    response_model=AnalysisStateResponse,
    summary="Fail an analysis with a readable reason",
    description=(
        "The reason is required. A failed analysis with no stated cause is the "
        "one nobody can act on, and it is the one the buyer asks about.\n\n"
        "An analysis that already ended is left as it is and its status comes "
        "back: a done analysis never becomes failed, and a failed one keeps the "
        "first reason."
    ),
)
async def fail_analysis(
    request_id: uuid.UUID, request: FailAnalysisRequest, session: AsyncSession = Depends(get_session)
) -> AnalysisStateResponse:
    analysis_request = await _load(session, request_id)
    await AnalysisRepository(session).mark_failed(analysis_request, error_message=request.error_message)
    await session.commit()
    return AnalysisStateResponse(request_id=request_id, status=analysis_request.status)


@router.post(
    "/weights/calibrate",
    response_model=CalibrateWeightsResponse,
    summary="Make sure the period has its dimension weights",
    description=(
        "The weights are deterministic — Rank Reciprocal over a fixed priority "
        "order, reading no country data — so this is safe to call before every "
        "scoring and cheap to call twice.\n\n"
        "Calling it is not optional. A period with no calibrated weight excludes "
        "every country from the MOS with the reason 'no calibrated weight', "
        "which reads like missing data and is not."
    ),
)
async def calibrate_weights(
    request: CalibrateWeightsRequest, session: AsyncSession = Depends(get_session)
) -> CalibrateWeightsResponse:
    weights = await WeightCalibrationService(session).calibrate(period_key=request.period_key)
    await session.commit()
    return CalibrateWeightsResponse(period_key=request.period_key, weights=weights)


@router.post(
    "/evidence",
    response_model=EvidenceResponse,
    summary="Build the deterministic evidence packet",
    description=(
        "Assembles what the narrative is written from and what the frontend "
        "renders: the glossary, the dimension weights, the demand ruler, and "
        "per country the MOS with every dimension subscore and scored signal.\n\n"
        "The countries are the requested ones plus the crowned champion when it "
        "was not requested (the one POST /internal/champion/crown stored), each "
        "flagged `requested` / `is_champion`, in `display_order`, with "
        "`headline_country`. Everything a reader sees is in `language`.\n\n"
        "Deterministic and free — it reads stored results and collects nothing. "
        "No model is involved, which is what makes the narrative checkable "
        "afterwards: a claim that is not in this packet is not supported.\n\n"
        "Returns null when no requested country has a stored MOS for the "
        "period. Absent is reported as absent, never as an empty packet."
    ),
)
async def build_evidence(request: EvidenceRequest, session: AsyncSession = Depends(get_session)) -> EvidenceResponse:
    packet = await AnalysisService(session).build_evidence(
        entity=await _entity(session, request.entity_key),
        requested=request.countries,
        period_key=request.period_key,
        language=request.language,
        demand_ruler=request.demand_ruler,
        marca=await _marca(session, request.brand_key),
    )
    return EvidenceResponse(packet=packet)


@router.post(
    "/narrate",
    response_model=NarrateResponse,
    summary="Turn an evidence packet into prose",
    description=(
        "The only place a model touches an analysis result, and it only "
        "explains: it never computes a score and never alters one.\n\n"
        "Best-effort. A model failure answers null rather than raising, because "
        "the numbers are the answer and a missing reading of them does not "
        "invalidate it."
    ),
)
async def narrate(request: NarrateRequest, session: AsyncSession = Depends(get_session)) -> NarrateResponse:
    narrative = await ExplanationClient(session).narrate(
        packet=request.packet,
        product_name=request.product_name,
        brand=request.brand,
        user_prompt=request.user_prompt,
        language=request.language,
    )
    return NarrateResponse(narrative=narrative)


# --- The steps that carry the analysis's own judgement ---


# A que distância do limiar ainda é "quase não passou". Medido em 23/09/2026:
# "cafetera italiana para espresso" casou com "cafeteira italiana moka" em
# 41,0% contra um corte de 40% — identidade de produto decidida por um fio.
_MARGEM_LIMIAR = 0.05


def _no_limiar(score: Decimal | None) -> bool:
    """Se esta coincidência passou raspando. Só o passo `trigram` tem score;
    nos outros não há nada de que estar perto."""
    if score is None:
        return False
    return float(score) < settings.catalog_trigram_similarity_threshold + _MARGEM_LIMIAR


async def _marca(session: AsyncSession, brand_key: uuid.UUID | None):  # noqa: ANN202
    """A marca desta análise, ou None.

    Resolvida aqui e passada adiante em vez de cada serviço buscar a sua: é ela
    que decide QUAL vocabulário a análise mede, e essa decisão precisa ser a
    mesma no enriquecimento, na cobertura e na régua de demanda.
    """
    if brand_key is None:
        return None
    return await session.get(Brand, brand_key)


async def _entity(session: AsyncSession, entity_key: uuid.UUID):  # noqa: ANN202
    entity = await ProductRepository(session).get_by_entity_key(entity_key)
    if entity is None:
        raise HTTPException(status_code=404, detail=f"Entity {entity_key} not found")
    return entity


@router.post(
    "/enrich-terms",
    response_model=EnrichTermsResponse,
    summary="Make sure the product has a search vocabulary in each country",
    description=(
        "Generates the per-country search terms and the reference-market "
        "candidates, and only for what is missing. A country that already has "
        "terms triggers nothing, and an entity that has everything costs no "
        "model call at all — reuse is what makes a repeated analysis fast and "
        "cheap.\n\n"
        "Must run before coverage, which reads these terms to know what to "
        "collect.\n\n"
        "For the generic family behind a branded product, pass "
        "with_candidates=false: the demand ruler belongs to the brand, and the "
        "category is context that is never scored."
    ),
)
async def enrich_terms(
    request: EnrichTermsRequest, session: AsyncSession = Depends(get_session)
) -> EnrichTermsResponse:
    entity = await _entity(session, request.entity_key)
    servico = AnalysisService(session)
    marca = await _marca(session, request.brand_key)
    await servico.ensure_product_enrichment(
        entity, request.countries, marca=marca, com_candidatos=request.with_candidates
    )
    await session.commit()
    markets = await servico.vocabulario(entity, marca)
    return EnrichTermsResponse(
        entity_key=request.entity_key,
        countries_with_terms=sorted(markets.keys()),
        # Os DESTA análise: numa de marca, os candidatos do produto contavam
        # zero ou os de outra pergunta.
        reference_candidates=len(await servico.candidatos_de_referencia(entity, marca)),
    )


@router.post(
    "/coverage/ensure",
    response_model=EnsureCoverageResponse,
    summary="Collect what this country is missing, and report what was tried",
    description=(
        "Checks what this country and product still need and dispatches "
        "collection only for the gaps. Already-fresh data is never "
        "re-collected, and for demand only genuinely new terms are downloaded.\n\n"
        "The dispatched tasks come back, and that list is the point. It is how "
        "the two reasons a country can still lack an indicator are told apart: "
        "a FAILED task is a source that is down and worth insisting on, while a "
        "task that finished without failing and still left the indicator "
        "missing is a source that does not have that datum. Insisting on the "
        "second never ends; declaring the first throws away a market over a "
        "thirty-minute outage."
    ),
)
async def ensure_coverage(
    request: EnsureCoverageRequest, session: AsyncSession = Depends(get_session)
) -> EnsureCoverageResponse:
    terms: dict[str, Any] = {}
    reuso: dict[str, Any] = {}
    tasks = await AnalysisService(session).ensure_coverage(
        country_iso3=request.country_iso3,
        entity_key=request.entity_key,
        period_key=request.period_key,
        marca=await _marca(session, request.brand_key),
        terms_used=terms,
        reuse_report=reuso,
    )
    await session.commit()
    return EnsureCoverageResponse(
        country=request.country_iso3,
        tasks=tasks,
        reused=reuso.get("reused", {}),
        related_fresh=bool(reuso.get("related_fresh")),
        not_covering=reuso.get("not_covering", []),
        location=terms.get("location"),
        language=terms.get("language"),
        search_terms=terms.get("search_terms", []),
        category_terms=terms.get("category_terms", []),
    )


@router.post(
    "/candidates/coverage",
    response_model=CandidatesCoverageResponse,
    summary="Collect demand for the product's reference markets",
    description=(
        "The demand ruler is the largest real search volume among the product's "
        "reference-market candidates, so those have to be collected regardless "
        "of which countries were asked about — that is what keeps the ruler "
        "stable instead of moving with each query.\n\n"
        "When champion_cached comes back true the collection was skipped: the "
        "period already has a champion computed against the CURRENT demand "
        "ruler. A champion computed against an older ruler does not count. "
        "champion_country/champion_mos are deprecated — crowning is POST "
        "/internal/champion/crown's.\n\n"
        "Candidate demand is "
        "the slowest collection in the system and re-running it produces the "
        "same answer: three consecutive analyses of one product took 594s, 203s "
        "and 396s, and nearly all of the repeat cost was this work redone."
    ),
)
async def candidates_coverage(
    request: CandidatesCoverageRequest, session: AsyncSession = Depends(get_session)
) -> CandidatesCoverageResponse:
    entity = await _entity(session, request.entity_key)
    marca = await _marca(session, request.brand_key)
    servico = AnalysisService(session)
    # O campeão guardado só poupa a coleta quando foi calculado contra a régua
    # de agora, medida do que já está guardado. Se a régua mudou, os candidatos
    # são coletados e o campeão é recalculado.
    teto, _regua, _novo = await servico.measure_demand_ceiling(
        entity, request.requested_countries, request.period_key, marca=marca
    )
    cached, pais, mos = await ProductRepository(session).champion_for(
        entity, marca.brand_key if marca is not None else None, request.period_key, demand_ceiling=teto
    )
    tarefas: list = []
    reuso: dict[str, Any] = {}
    if not cached:
        tarefas = await servico.ensure_candidates_coverage(
            entity,
            request.requested_countries,
            period_key=request.period_key,
            marca=marca,
            reuse_report=reuso,
        )
        await session.commit()
    return CandidatesCoverageResponse(
        entity_key=request.entity_key,
        tasks=tarefas,
        reused=reuso.get("reused", {}),
        related_fresh=int(reuso.get("related_fresh") or 0),
        champion_cached=cached,
        # O da marca quando há marca: devolver `entity.champion_*` coroava o
        # campeão do PRODUTO numa análise de marca.
        champion_country=pais if cached else None,
        champion_mos=float(mos) if cached and mos is not None else None,
    )


@router.post(
    "/demand-ceiling",
    response_model=DemandCeilingResponse,
    summary="Resolve the ruler the country's demand is measured against",
    description=(
        "Reads the largest real search volume among the product's reference "
        "markets. It only reads — it never collects — so it returns whatever "
        "the last candidate collection left behind, which is what makes the "
        "ruler stable across repeats rather than drifting.\n\n"
        "Falls back to the largest requested-country volume, then to a fixed "
        "bound. Which one was used comes back in demand_ruler and belongs in "
        "the narrative: a normalised figure presented as an absolute one is a "
        "false claim, not a rounding."
    ),
)
async def demand_ceiling(
    request: DemandCeilingRequest, session: AsyncSession = Depends(get_session)
) -> DemandCeilingResponse:
    entity = await _entity(session, request.entity_key)
    ceiling, ruler = await AnalysisService(session).resolve_demand_ceiling(
        entity, request.countries, request.period_key, marca=await _marca(session, request.brand_key)
    )
    return DemandCeilingResponse(demand_ceiling=ceiling, demand_ruler=ruler)


@router.post(
    "/candidates/score",
    response_model=ScoreCandidatesResponse,
    summary="Score the product's reference markets",
    deprecated=True,
    description=(
        "DEPRECATED — POST /internal/champion/crown scores the candidates and "
        "crowns the champion in one call.\n\n"
        "Scores the reference-market candidates the caller did not ask about. "
        "The MOS uses fixed anchors, so this never changes what a requested "
        "country scores — which is what makes it safe to surface the best of "
        "them as a benchmark.\n\n"
        "Their demand is already collected by then, so this adds only free "
        "macro data and arithmetic."
    ),
)
async def score_candidates(
    request: ScoreCandidatesRequest, session: AsyncSession = Depends(get_session)
) -> ScoreCandidatesResponse:
    entity = await _entity(session, request.entity_key)
    results = await AnalysisService(session).score_reference_candidates(
        entity=entity,
        requested=request.requested_countries,
        period_key=request.period_key,
        demand_ceiling=request.demand_ceiling,
        marca=await _marca(session, request.brand_key),
    )
    await session.commit()
    return ScoreCandidatesResponse(
        candidates=[
            ScoredCandidate(
                country=row.country,
                mos=None if row.mos is None else float(row.mos),
                availability_status=row.availability_status,
                excluded_reason=row.excluded_reason,
            )
            for row in results
        ]
    )


@router.post(
    "/champion",
    response_model=AnalysisStateResponse,
    summary="Record the product's best known market for a period",
    deprecated=True,
    description=(
        "DEPRECATED — POST /internal/champion/crown decides and stores the "
        "champion itself.\n\n"
        "Stores which market this product scores highest in, so a later "
        "analysis reuses it instead of recollecting the candidates.\n\n"
        "A null country is a real outcome and must be recorded: it means "
        "nothing was scorable this period, and without storing that the same "
        "fruitless work is attempted again on every later analysis.\n\n"
        "Store the best REQUESTED market when it beats the stored champion. "
        "The stored value means 'this product's best known market', so leaving "
        "a beaten one in place keeps it wrong for every analysis after."
    ),
)
async def set_champion(
    request: SetChampionRequest, session: AsyncSession = Depends(get_session)
) -> AnalysisStateResponse:
    entity = await _entity(session, request.entity_key)
    marca = await _marca(session, request.brand_key)
    # A régua de agora vai junto: é o que diz para qual régua este campeão vale.
    teto, _regua, _novo = await AnalysisService(session).measure_demand_ceiling(
        entity, [request.country] if request.country else [], request.period_key, marca=marca
    )
    await ProductRepository(session).set_champion(
        entity,
        country=request.country,
        mos=None if request.mos is None else Decimal(str(request.mos)),
        period_key=request.period_key,
        demand_ceiling=teto,
        brand_key=marca.brand_key if marca is not None else None,
    )
    await session.commit()
    return AnalysisStateResponse(request_id=request.entity_key, status="stored")


@router.post(
    "/champion/crown",
    response_model=CrownChampionResponse,
    summary="Crown the product's best market, requested or not",
    description=(
        "Given the requested countries' MOS rows, answers which market is the "
        "product's best for the period — one of the requested ones or a "
        "reference candidate nobody asked about — and stores it.\n\n"
        "Decided here because the ruler is here: the stored champion is reused "
        "only when it was computed against the current demand ruler, otherwise "
        "the candidates are scored again. Ties go to the requested market. "
        "When the champion was not requested, its MOS row comes back in "
        "`champion_row`, ready to be delivered next to the requested rows.\n\n"
        "Call it after POST /mos/compute and before building the evidence: the "
        "evidence packet reads the champion stored here."
    ),
)
async def crown_champion(
    request: CrownChampionRequest, session: AsyncSession = Depends(get_session)
) -> CrownChampionResponse:
    entity = await _entity(session, request.entity_key)
    coroado = await AnalysisService(session).crown_champion(
        entity=entity,
        requested=request.requested_countries,
        requested_results=request.requested_results,
        period_key=request.period_key,
        marca=await _marca(session, request.brand_key),
    )
    return CrownChampionResponse(
        champion_country=coroado.country,
        champion_mos=coroado.mos,
        requested=coroado.requested,
        champion_row=_linha_de_mos(coroado.row) if coroado.row is not None else None,
        from_cache=coroado.from_cache,
        demand_ceiling=coroado.demand_ceiling,
    )


def _linha_de_mos(row: Any) -> dict[str, Any]:  # noqa: ANN401
    """Uma linha de MOS no formato de POST /mos/compute."""
    return MosResultResponse.model_validate(row, from_attributes=True).model_dump(mode="json")


@router.post(
    "/markets/demand-absence",
    response_model=DemandAbsenceResponse,
    summary="Which markets had no demand for this product and brand, recently",
    description=(
        "For each country, whether every search term of this product (and "
        "brand) was answered by the demand source inside the demand freshness "
        "window and none has volume that counts. A country with a term still "
        "to collect answers absent=false: that is not knowing, not barren.\n\n"
        "Read from the stored data, so it heals itself: a source that gains "
        "volume answers absent=false on the next call, and the window is the "
        "demand's own. Free — nothing is collected."
    ),
)
async def demand_absence(
    request: DemandAbsenceRequest, session: AsyncSession = Depends(get_session)
) -> DemandAbsenceResponse:
    entity = await _entity(session, request.entity_key)
    marca = await _marca(session, request.brand_key)
    servico = AnalysisService(session)
    return DemandAbsenceResponse(
        markets=[
            DemandAbsence(**await servico.demand_absence(entity, pais, period_key=request.period_key, marca=marca))
            for pais in request.countries
        ],
        window_days=settings.demand_stale_after_days,
    )
