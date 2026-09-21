import asyncio

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_session
from app.domain.enums import DataSource
from app.schemas.responses import ExternalSourceStatus, HealthResponse

router = APIRouter()

# Short on purpose. This answers "can I reach it", and a collection service that
# takes longer than this to say hello is not one an analysis should wait on.
_PROBE_TIMEOUT = httpx.Timeout(5.0, connect=2.0)

_BASE_URLS: dict[DataSource, str] = {
    DataSource.PYTRENDS: settings.pytrends_api_base_url,
    DataSource.WORLDBANK: settings.worldbank_api_base_url,
    DataSource.OECD: settings.oecd_api_base_url,
    DataSource.UNDATA: settings.undata_api_base_url,
}


async def _probe(source: DataSource) -> ExternalSourceStatus:
    """Reach a collection service the same way the rest of this service does.

    This used to open a read-only SQL connection to that service's database,
    which reported "connected" for a reachable database behind an application
    that was down or broken -- and once for a database that was reachable but
    was the wrong one entirely, because two services had their ports swapped.
    Probing the API means a green health check means the path the data actually
    travels is working.
    """
    url = f"{_BASE_URLS[source].rstrip('/')}/health"
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as client:
            response = await client.get(url)
            response.raise_for_status()
        return ExternalSourceStatus(source=source.value, status="connected")
    except Exception:  # noqa: BLE001 — a health probe reports, it never raises
        return ExternalSourceStatus(source=source.value, status="disconnected")


@router.get(
    "/healthz",
    summary="Is this process alive",
    description=(
        "Answers immediately, reads nothing, and calls nobody. This is what a "
        "liveness probe is for: whether THIS process can still answer.\n\n"
        "GET /health is a different question and must not be used for that. It "
        "reaches the four collection services over HTTP, so a slow PyTrends "
        "makes it slow — and a liveness probe pointed at it restarts this "
        "service because a different service is unwell. Restarting fixes "
        "nothing there and costs something real: an analysis running in this "
        "process dies with it, and comes back only when the resume sweep "
        "notices fifteen minutes later. That is what was happening."
    ),
)
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Health Check",
    description=(
        "Returns application status, own database connectivity, and HTTP "
        "reachability of the 4 collection services (PyTrends, WorldBank, OECD, "
        "UNData) — the same path their data is read over."
    ),
)
async def health_check(session: AsyncSession = Depends(get_session)) -> HealthResponse:
    try:
        await session.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception:  # noqa: BLE001 — a health probe reports, it never raises
        db_status = "disconnected"

    # Probed together: four sequential five-second timeouts would make a health
    # check take twenty seconds to report that everything is down.
    external_statuses = list(await asyncio.gather(*(_probe(source) for source in DataSource)))

    return HealthResponse(
        status="ok",
        database=db_status,
        external_sources=external_statuses,
        app_name=settings.app_name,
        environment=settings.app_env,
    )
