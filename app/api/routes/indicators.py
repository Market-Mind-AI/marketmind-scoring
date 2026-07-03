from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import EntityNotFoundError, MarketMindCoreError
from app.db.session import get_session
from app.schemas.indicators import ComputeIndicatorsRequest, ComputeIndicatorsResponse, IndicatorValueResponse
from app.services.indicator_engine import IndicatorEngine

router = APIRouter()


@router.post(
    "/indicators/compute",
    response_model=ComputeIndicatorsResponse,
    summary="Compute Cap. 4 indicators",
    description=(
        "Computes every applicable indicator for a country (and, if "
        "entity_key is given, the 5 product-specific PyTrends indicators "
        "plus the conditional Median Age Fit too), normalizes each against "
        "normalization_reference, and upserts into indicator_values."
    ),
)
async def compute_indicators(
    request: ComputeIndicatorsRequest, session: AsyncSession = Depends(get_session)
) -> ComputeIndicatorsResponse:
    engine = IndicatorEngine(session)
    try:
        results = await engine.compute_all(
            country_iso3=request.country, entity_key=request.entity_key, period_key=request.period_key
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except MarketMindCoreError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return ComputeIndicatorsResponse(
        results=[IndicatorValueResponse.model_validate(r, from_attributes=True) for r in results]
    )
