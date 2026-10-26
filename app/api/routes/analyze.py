import asyncio
import json
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.analysis_request import AnalysisRequest
from app.db.session import async_session_factory, get_session
from app.repositories.analysis_repository import AnalysisRepository
from app.repositories.mos_repository import MosRepository
from app.repositories.product_repository import ProductRepository
from app.schemas.analyze import AnalysisStatusResponse, AnalyzeAcceptedResponse, AnalyzeRequest
from app.services.analysis_service import AnalysisService
from app.services.catalog_service import CatalogService
from app.services.explanation_client import ExplanationClient

router = APIRouter()

# SSE poll cadence and safety cap. The run happens in the orchestrator; this
# side just watches the DB row and pushes on change — free, no message broker.
# The cap bounds a stream watching a request that stops moving.
_SSE_POLL_SECONDS = 1.5
_SSE_MAX_SECONDS = 15 * 60
# How long the stream keeps watching AFTER the scores are done, waiting for the
# narrative. The narrative is one model call and the request is already complete
# without it, so this is bounded and short: past it the stream closes saying the
# text did not arrive, rather than holding a connection open for a sentence.
_SSE_NARRATIVE_SECONDS = 180.0


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
        # A marca DESTA análise: sem ela, um relatório de marca seria
        # remontado com o MOS do produto.
        row = await mos_repo.get_mos_result(
            analysis_request.entity_key, country, "month", period_key, analysis_request.brand_key
        )
        if row is not None:
            mos_results.append(row)
    if not mos_results:
        return None
    return await ExplanationClient(session).build_evidence(
        entity_key=analysis_request.entity_key, mos_results=mos_results, period_key=period_key
    )


async def _identidade(session: AsyncSession, analysis_request: AnalysisRequest) -> tuple[str | None, str | None]:
    """(marca, título da categoria) da entidade analisada.

    O `canonical_title` gravado na análise já carrega a marca ("Cardhu single
    malt Scotch whisky"), mas o relatório precisa das duas partes separadas —
    para exibir "Cardhu · single malt Scotch whisky" e para dar nome ao sinal de
    contexto `category_volume_score` do breakdown — e fatiar string para obter
    isso seria frágil. Lido da entidade, que é onde a partição vive.
    """
    if analysis_request.entity_key is None:
        return None, None
    repo = ProductRepository(session)
    entity = await repo.get_by_entity_key(analysis_request.entity_key)
    if entity is None:
        return None, None
    marca = await repo.get_brand(entity.brand_key)
    familia = await repo.get_by_entity_key(entity.generic_entity_key) if entity.generic_entity_key else None
    return (
        marca.name if marca is not None else None,
        familia.canonical_title if familia is not None else None,
    )


def _status_payload(
    analysis_request: AnalysisRequest,
    breakdown: dict[str, Any] | None,
    brand: str | None = None,
    category_title: str | None = None,
) -> AnalysisStatusResponse:
    return AnalysisStatusResponse(
        request_id=analysis_request.id,
        status=analysis_request.status,
        canonical_title=analysis_request.canonical_title,
        brand=brand,
        category_title=category_title,
        user_prompt=analysis_request.user_prompt,
        result=analysis_request.result,
        explanation=analysis_request.explanation,
        breakdown=breakdown,
        error_message=analysis_request.error_message,
        progress=analysis_request.progress,
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
        "and computing the Cap. 5 MOS — is done by the orchestrator, which "
        "this hands the request to. Poll GET /analyze/{request_id} for the "
        "result, or subscribe to GET /analyze/{request_id}/stream (SSE) for "
        "live status."
    ),
)
async def analyze(
    request: AnalyzeRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> AnalyzeAcceptedResponse:
    catalog_service = CatalogService(session)
    # A marca PARTICIONA a identidade: um pedido com marca resolve para a
    # entidade daquela marca (título com a marca), com a família genérica
    # ligada por trás. Ver catalog_service.resolve.
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
    # compartilhado — se fizer sentido para este produto. Ver revisar_edicoes.
    await catalog_service.revisar_edicoes(
        resolved.entity_key,
        category=request.category,
        target_age_min=request.target_age_min,
        target_age_max=request.target_age_max,
        estimated_price=request.estimated_price,
        price_currency=request.price_currency,
    )

    analysis_repo = AnalysisRepository(session)
    analysis_request = await analysis_repo.create(
        entity_key=resolved.entity_key,
        canonical_title=resolved.canonical_title,
        countries=request.countries,
        estimated_price=request.estimated_price,
        price_currency=request.price_currency,
        user_prompt=request.user_prompt,
        language=request.language,
    )
    await session.commit()

    background_tasks.add_task(AnalysisService(session).run, analysis_request.id)

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
async def get_analysis(
    request_id: str, response: Response, session: AsyncSession = Depends(get_session)
) -> AnalysisStatusResponse:
    parsed_id = _parse_request_id(request_id)
    analysis_repo = AnalysisRepository(session)
    analysis_request = await analysis_repo.get(parsed_id)
    if analysis_request is None:
        raise HTTPException(status_code=404, detail=f"Analysis request {request_id} not found")

    # This URL is polled while the answer is still changing and never changes
    # shape, so anything between here and the browser is free to serve the first
    # body it saw — which reads to the user as an analysis that finished long
    # after it did. There is nothing here worth caching: it is one row.
    response.headers["Cache-Control"] = "no-store"

    breakdown = await _load_breakdown(session, analysis_request)
    brand, category_title = await _identidade(session, analysis_request)
    return _status_payload(analysis_request, breakdown, brand, category_title)


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
        last: tuple[str, str | None, bool] | None = None
        waited = 0.0
        done_since: float | None = None
        while True:
            if await request.is_disconnected():
                return
            async with async_session_factory() as session:
                analysis_request = await AnalysisRepository(session).get(parsed_id)
            if analysis_request is None:
                yield f"data: {json.dumps({'request_id': request_id, 'status': 'not_found'})}\n\n"
                return

            status = analysis_request.status
            progress = analysis_request.progress
            has_explanation = bool(analysis_request.explanation and analysis_request.explanation.strip())

            # Pushed on ANY of the three changing, not on the status alone. The
            # progress line is what the waiting screen says it is doing, and it
            # moves many times within one status; watching only the status meant
            # the caller had to poll for it anyway, which is what this endpoint
            # exists to avoid.
            current = (status, progress, has_explanation)
            if current != last:
                last = current
                yield "data: " + json.dumps(
                    {
                        "request_id": request_id,
                        "status": status,
                        "progress": progress,
                        "has_explanation": has_explanation,
                    }
                ) + "\n\n"

            if status == "failed":
                return
            if status == "done":
                # NOT closed the moment the scores land. The narrative is
                # attached a moment later, and a stream that closes here leaves
                # the caller polling for the slowest part of the run — which is
                # exactly the wait people notice, because the answer is already
                # sitting in the database while the screen still says it is
                # being written.
                if has_explanation:
                    return
                done_since = waited if done_since is None else done_since
                if waited - done_since >= _SSE_NARRATIVE_SECONDS:
                    yield "data: " + json.dumps(
                        {
                            "request_id": request_id,
                            "status": status,
                            "progress": progress,
                            "has_explanation": False,
                            "narrative_abandoned": True,
                        }
                    ) + "\n\n"
                    return
            elif waited >= _SSE_MAX_SECONDS:
                yield "data: " + json.dumps(
                    {"request_id": request_id, "status": status, "progress": progress, "timeout": True}
                ) + "\n\n"
                return

            # Comment line doubles as an SSE heartbeat, keeping proxies from
            # closing an idle connection between changes.
            yield ": ping\n\n"
            await asyncio.sleep(_SSE_POLL_SECONDS)
            waited += _SSE_POLL_SECONDS

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
