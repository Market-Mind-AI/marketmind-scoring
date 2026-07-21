import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.repositories.analysis_repository import AnalysisRepository
from app.schemas.analyze import AnalysisStatusResponse, AnalyzeAcceptedResponse, AnalyzeRequest
from app.services.analysis_service import AnalysisService
from app.services.catalog_service import CatalogService

router = APIRouter()


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
        "GET /analyze/{request_id} for the result. v1 uses FastAPI "
        "BackgroundTasks (no dedicated worker/queue yet) — if the API "
        "restarts mid-run, the request is left stuck in 'running'."
    ),
)
async def analyze(
    request: AnalyzeRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> AnalyzeAcceptedResponse:
    catalog_service = CatalogService(session)
    resolve_text = f"{request.brand} {request.product_name}".strip() if request.brand else request.product_name
    resolved = await catalog_service.resolve(
        resolve_text,
        source="analyze",
        category=request.category,
        localized_markets=request.localized_markets,
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
    )
    await session.commit()

    analysis_service = AnalysisService(session)
    background_tasks.add_task(analysis_service.run, analysis_request.id)

    return AnalyzeAcceptedResponse(request_id=analysis_request.id, status=analysis_request.status)


@router.get(
    "/analyze/{request_id}",
    response_model=AnalysisStatusResponse,
    summary="Poll an analysis request",
    description="result is only populated once status == 'done'.",
)
async def get_analysis(request_id: str, session: AsyncSession = Depends(get_session)) -> AnalysisStatusResponse:
    try:
        parsed_id = uuid.UUID(request_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="request_id must be a UUID") from exc

    analysis_repo = AnalysisRepository(session)
    analysis_request = await analysis_repo.get(parsed_id)
    if analysis_request is None:
        raise HTTPException(status_code=404, detail=f"Analysis request {request_id} not found")

    return AnalysisStatusResponse(
        request_id=analysis_request.id,
        status=analysis_request.status,
        canonical_title=analysis_request.canonical_title,
        result=analysis_request.result,
        explanation=analysis_request.explanation,
        error_message=analysis_request.error_message,
    )
