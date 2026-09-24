from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import EntityNotFoundError, MarketMindCoreError
from app.db.session import get_session
from app.schemas.mos import ComputeMosRequest, ComputeMosResponse, MosResultResponse
from app.services.mos_engine import MosEngine

router = APIRouter()


@router.post(
    "/mos/compute",
    response_model=ComputeMosResponse,
    summary="Compute MOS (Cap. 5)",
    description=(
        "Computes MOS for a product across the given countries, one "
        "independent result per country (never aggregated). Requires "
        "indicator_values (Fase 3) and calibrated dimension weights "
        "(POST /admin/mos/refresh-weights) for the resolved period_key to "
        "already exist — this endpoint does not trigger collection or "
        "calibration itself."
    ),
)
async def compute_mos(request: ComputeMosRequest, session: AsyncSession = Depends(get_session)) -> ComputeMosResponse:
    engine = MosEngine(session)
    try:
        results = await engine.compute_mos(
            entity_key=request.entity_key,
            countries=request.countries,
            period_key=request.period_key,
            brand_key=request.brand_key,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except MarketMindCoreError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return ComputeMosResponse(results=[MosResultResponse.model_validate(r, from_attributes=True) for r in results])
