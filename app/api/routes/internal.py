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

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_internal_token
from app.core.logging import logger
from app.db.session import get_session
from app.repositories.analysis_repository import AnalysisRepository
from app.repositories.mos_repository import MosRepository
from app.repositories.product_repository import ProductRepository
from app.schemas.internal import (
    AnalysisDetailResponse,
    AnalysisStateResponse,
    CalibrateWeightsRequest,
    CalibrateWeightsResponse,
    CandidatesCoverageRequest,
    CandidatesCoverageResponse,
    CompleteAnalysisRequest,
    DemandCeilingRequest,
    DemandCeilingResponse,
    EnrichTermsRequest,
    EnrichTermsResponse,
    EnsureCoverageRequest,
    EnsureCoverageResponse,
    EvidenceRequest,
    EvidenceResponse,
    FailAnalysisRequest,
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
from app.services.analysis_service import AnalysisService
from app.services.explanation_client import ExplanationClient
from app.services.weight_calibration_service import WeightCalibrationService
from app.utils.time import utc_now

router = APIRouter(prefix="/internal", tags=["Internal"], dependencies=[Depends(require_internal_token)])


async def _load(session: AsyncSession, request_id: uuid.UUID):  # noqa: ANN202
    analysis_request = await AnalysisRepository(session).get(request_id)
    if analysis_request is None:
        raise HTTPException(status_code=404, detail=f"Analysis request {request_id} not found")
    return analysis_request


@router.post(
    "/analyses",
    response_model=RegisteredAnalysisResponse,
    status_code=201,
    summary="Register an analysis",
    description=(
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
    )
    await session.commit()
    return RegisteredAnalysisResponse(request_id=analysis_request.id, status=analysis_request.status)


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
async def read_analysis(
    request_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> AnalysisDetailResponse:
    analysis_request = await _load(session, request_id)
    return AnalysisDetailResponse(
        request_id=analysis_request.id,
        status=analysis_request.status,
        entity_key=analysis_request.entity_key,
        canonical_title=analysis_request.canonical_title,
        countries=list(analysis_request.countries or []),
        user_prompt=analysis_request.user_prompt,
        progress=analysis_request.progress,
        explanation=analysis_request.explanation,
        error_message=analysis_request.error_message,
        result=analysis_request.result,
    )


@router.post(
    "/analyses/{request_id}/running",
    response_model=AnalysisStateResponse,
    summary="Mark an analysis as running",
)
async def mark_running(request_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> AnalysisStateResponse:
    analysis_request = await _load(session, request_id)
    repo = AnalysisRepository(session)
    await repo.mark_running(analysis_request)
    analysis_request.heartbeat_at = utc_now()
    await session.commit()
    return AnalysisStateResponse(request_id=analysis_request.id, status=analysis_request.status)


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
        "once took longer than the entire rest of the analysis."
    ),
)
async def complete_analysis(
    request_id: uuid.UUID, request: CompleteAnalysisRequest, session: AsyncSession = Depends(get_session)
) -> AnalysisStateResponse:
    analysis_request = await _load(session, request_id)
    await AnalysisRepository(session).mark_done(
        analysis_request,
        result=request.result,
        explanation=request.explanation,
        breakdown=request.breakdown,
    )
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
        "one nobody can act on, and it is the one the buyer asks about."
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
        "Deterministic and free — it reads stored results and collects nothing. "
        "No model is involved, which is what makes the narrative checkable "
        "afterwards: a claim that is not in this packet is not supported.\n\n"
        "Returns null when no requested country has a stored MOS for the "
        "period. Absent is reported as absent, never as an empty packet."
    ),
)
async def build_evidence(
    request: EvidenceRequest, session: AsyncSession = Depends(get_session)
) -> EvidenceResponse:
    mos_repo = MosRepository(session)
    mos_results = []
    for country in request.countries:
        row = await mos_repo.get_mos_result(request.entity_key, country, "month", request.period_key)
        if row is not None:
            mos_results.append(row)
    if not mos_results:
        return EvidenceResponse(packet=None)

    packet = await ExplanationClient(session).build_evidence(
        entity_key=request.entity_key,
        mos_results=mos_results,
        period_key=request.period_key,
        demand_ruler=request.demand_ruler,
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
        user_prompt=request.user_prompt,
    )
    return NarrateResponse(narrative=narrative)


# --- The steps that carry the analysis's own judgement ---


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
    await AnalysisService(session).ensure_product_enrichment(
        entity, request.countries, com_candidatos=request.with_candidates
    )
    await session.commit()
    markets = entity.localized_markets or {}
    return EnrichTermsResponse(
        entity_key=request.entity_key,
        countries_with_terms=sorted(markets.keys()),
        reference_candidates=len(entity.reference_candidates or []),
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
    tasks = await AnalysisService(session).ensure_coverage(
        country_iso3=request.country_iso3, entity_key=request.entity_key
    )
    await session.commit()
    return EnsureCoverageResponse(country=request.country_iso3, tasks=tasks)


@router.post(
    "/candidates/coverage",
    response_model=CandidatesCoverageResponse,
    summary="Collect demand for the product's reference markets",
    description=(
        "The demand ruler is the largest real search volume among the product's "
        "reference-market candidates, so those have to be collected regardless "
        "of which countries were asked about — that is what keeps the ruler "
        "stable instead of moving with each query.\n\n"
        "Skip this when champion_cached is already true. Candidate demand is "
        "the slowest collection in the system and re-running it produces the "
        "same answer: three consecutive analyses of one product took 594s, 203s "
        "and 396s, and nearly all of the repeat cost was this work redone."
    ),
)
async def candidates_coverage(
    request: CandidatesCoverageRequest, session: AsyncSession = Depends(get_session)
) -> CandidatesCoverageResponse:
    entity = await _entity(session, request.entity_key)
    cached = entity.champion_period_key == request.period_key
    if not cached:
        await AnalysisService(session).ensure_candidates_coverage(
            entity, request.requested_countries, period_key=request.period_key
        )
        await session.commit()
    return CandidatesCoverageResponse(entity_key=request.entity_key, champion_cached=cached)


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
        entity, request.countries, request.period_key
    )
    return DemandCeilingResponse(demand_ceiling=ceiling, demand_ruler=ruler)


@router.post(
    "/candidates/score",
    response_model=ScoreCandidatesResponse,
    summary="Score the product's reference markets",
    description=(
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
    description=(
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
    await ProductRepository(session).set_champion(
        entity,
        country=request.country,
        mos=None if request.mos is None else Decimal(str(request.mos)),
        period_key=request.period_key,
    )
    await session.commit()
    return AnalysisStateResponse(request_id=request.entity_key, status="stored")
