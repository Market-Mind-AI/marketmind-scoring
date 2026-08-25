import asyncio
import json
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.analysis_request import AnalysisRequest
from app.db.session import async_session_factory, get_session
from app.repositories.analysis_repository import AnalysisRepository
from app.repositories.mos_repository import MosRepository
from app.schemas.analyze import AnalysisStatusResponse, AnalyzeAcceptedResponse, AnalyzeRequest
from app.services.analysis_service import AnalysisService
from app.services.catalog_service import CatalogService
from app.services.explanation_client import ExplanationClient

router = APIRouter()

# SSE poll cadence and safety cap. The pipeline runs as a FastAPI BackgroundTask
# in-process, so the stream just watches the DB row and pushes on change — free,
# no message broker. The cap bounds a stuck-in-"running" request (v1 has no
# worker/retry, see AnalysisRequest docstring).
_SSE_POLL_SECONDS = 1.5
_SSE_MAX_SECONDS = 15 * 60


async def _load_breakdown(session: AsyncSession, analysis_request: AnalysisRequest) -> dict[str, Any] | None:
    """Return the stored evidence packet, or rebuild it lazily (free — DB reads
    only, no collection) for analyses done before the `breakdown` column existed.
    The lazy rebuild omits the demand ruler (not persisted historically)."""
    if analysis_request.breakdown is not None:
        return analysis_request.breakdown
    if (
        analysis_request.status != "done"
        or analysis_request.entity_key is None
        or not analysis_request.result
    ):
        return None
    period_key = analysis_request.result[0].get("period_key")
    if not period_key:
        return None
    mos_repo = MosRepository(session)
    mos_results = []
    for country in analysis_request.countries:
        row = await mos_repo.get_mos_result(analysis_request.entity_key, country, "month", period_key)
        if row is not None:
            mos_results.append(row)
    if not mos_results:
        return None
    return await ExplanationClient(session).build_evidence(
        entity_key=analysis_request.entity_key, mos_results=mos_results, period_key=period_key
    )


def _status_payload(analysis_request: AnalysisRequest, breakdown: dict[str, Any] | None) -> AnalysisStatusResponse:
    return AnalysisStatusResponse(
        request_id=analysis_request.id,
        status=analysis_request.status,
        canonical_title=analysis_request.canonical_title,
        user_prompt=analysis_request.user_prompt,
        result=analysis_request.result,
        explanation=analysis_request.explanation,
        breakdown=breakdown,
        error_message=analysis_request.error_message,
    )


@router.post(
    "/analyze",
    response_model=AnalyzeAcceptedResponse,
    status_code=202,
    summary="Analyze a product across countries (async)",
    description=(
        "Resolves the product (Fase 2, same pipeline as POST /catalog/resolve "
        "and POST /catalog/identify-image), then returns a request_id "
        "immediately (202 Accepted) and processes everything else — ensuring "
        "the last 6 months of data freshness, computing Cap. 4 indicators, "
        "and computing the Cap. 5 MOS — in the background. Poll "
        "GET /analyze/{request_id} for the result, or subscribe to "
        "GET /analyze/{request_id}/stream (SSE) for live status. v1 uses FastAPI "
        "BackgroundTasks (no dedicated worker/queue yet) — if the API "
        "restarts mid-run, the request is left stuck in 'running'."
    ),
)
async def analyze(
    request: AnalyzeRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> AnalyzeAcceptedResponse:
    from app.core.config import settings
    catalog_service = CatalogService(session)
    # Identidade pelo nome genérico; a marca é acumulada na entidade, não
    # concatenada ao texto (ver catalog_service.resolve).
    resolved = await catalog_service.resolve(
        request.product_name,
        source="analyze",
        brand=request.brand,
        category=request.category,
        localized_markets=request.localized_markets,
        reference_candidates=request.reference_candidates,
        target_age_min=request.target_age_min,
        target_age_max=request.target_age_max,
    )

    analysis_repo = AnalysisRepository(session)
    analysis_request = await analysis_repo.create(
        entity_key=resolved.entity_key,
        canonical_title=resolved.canonical_title,
        countries=request.countries,
        estimated_price=request.estimated_price,
        price_currency=request.price_currency,
        user_prompt=request.user_prompt,
    )
    await session.commit()

    analysis_service = AnalysisService(session)
    if settings.use_new_orchestrator:
        background_tasks.add_task(analysis_service.run_via_orchestrator, analysis_request.id)
    else:
        background_tasks.add_task(analysis_service.run, analysis_request.id)

    return AnalyzeAcceptedResponse(request_id=analysis_request.id, status=analysis_request.status)


def _parse_request_id(request_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(request_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="request_id must be a UUID") from exc


@router.get(
    "/analyze/{request_id}",
    response_model=AnalysisStatusResponse,
    summary="Poll an analysis request",
    description="result/breakdown are only populated once status == 'done'.",
)
async def get_analysis(request_id: str, session: AsyncSession = Depends(get_session)) -> AnalysisStatusResponse:
    parsed_id = _parse_request_id(request_id)
    analysis_repo = AnalysisRepository(session)
    analysis_request = await analysis_repo.get(parsed_id)
    if analysis_request is None:
        raise HTTPException(status_code=404, detail=f"Analysis request {request_id} not found")

    breakdown = await _load_breakdown(session, analysis_request)
    return _status_payload(analysis_request, breakdown)


@router.get(
    "/analyze/{request_id}/stream",
    summary="Live status stream (SSE)",
    description=(
        "Server-Sent Events stream of an analysis request's status. Emits a "
        "JSON event on every status change and closes once the request reaches "
        "a terminal state ('done' or 'failed'). Free push (no broker) — the "
        "server watches the DB row. Use EventSource in the browser."
    ),
)
async def stream_analysis(request_id: str, request: Request) -> StreamingResponse:
    parsed_id = _parse_request_id(request_id)

    async def event_stream() -> AsyncGenerator[str, None]:
        last_status: str | None = None
        waited = 0.0
        while True:
            if await request.is_disconnected():
                return
            async with async_session_factory() as session:
                analysis_request = await AnalysisRepository(session).get(parsed_id)
            if analysis_request is None:
                yield f"data: {json.dumps({'request_id': request_id, 'status': 'not_found'})}\n\n"
                return
            status = analysis_request.status
            if status != last_status:
                last_status = status
                yield f"data: {json.dumps({'request_id': request_id, 'status': status})}\n\n"
            if status in ("done", "failed"):
                return
            if waited >= _SSE_MAX_SECONDS:
                yield f"data: {json.dumps({'request_id': request_id, 'status': status, 'timeout': True})}\n\n"
                return
            # Comment line doubles as an SSE heartbeat, keeping proxies from
            # closing an idle connection between status changes.
            yield ": ping\n\n"
            await asyncio.sleep(_SSE_POLL_SECONDS)
            waited += _SSE_POLL_SECONDS

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
