from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.external import get_external_engine
from app.db.session import get_session
from app.domain.enums import DataSource
from app.schemas.responses import ExternalSourceStatus, HealthResponse

router = APIRouter()


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Health Check",
    description=(
        "Returns application status, own database connectivity, and read-only "
        "connectivity to the 4 collection services' databases (PyTrends, "
        "WorldBank, OECD, UNData)."
    ),
)
async def health_check(session: AsyncSession = Depends(get_session)) -> HealthResponse:
    try:
        await session.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception:
        db_status = "disconnected"

    external_statuses: list[ExternalSourceStatus] = []
    for source in DataSource:
        try:
            engine = get_external_engine(source)
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            external_statuses.append(ExternalSourceStatus(source=source.value, status="connected"))
        except Exception:
            external_statuses.append(ExternalSourceStatus(source=source.value, status="disconnected"))

    return HealthResponse(
        status="ok",
        database=db_status,
        external_sources=external_statuses,
        app_name=settings.app_name,
        environment=settings.app_env,
    )
