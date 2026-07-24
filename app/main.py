from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes.admin import router as admin_router
from app.api.routes.analyze import router as analyze_router
from app.api.routes.catalog import router as catalog_router
from app.api.routes.coverage import router as coverage_router
from app.api.routes.health import router as health_router
from app.api.routes.indicators import router as indicators_router
from app.api.routes.mos import router as mos_router
from app.core.config import settings
from app.core.logging import logger
from app.db.external import dispose_all_external_engines
from app.db.session import engine


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info(f"Starting {settings.app_name} in {settings.app_env} mode")
    yield
    await engine.dispose()
    await dispose_all_external_engines()
    logger.info(f"Shutting down {settings.app_name}")


app = FastAPI(
    title="MarketMind Core API",
    description=(
        "Orchestration service for MarketMind. Owns the product catalog, the "
        "coverage index (what data has already been collected and for which "
        "period), the deterministic indicator engine (Cap. 4) and the MOS "
        "engine (Cap. 5 — Entropy Weight + TOPSIS, computed per country, "
        "never aggregated across countries). Reads collection services' "
        "databases read-only and never writes to them. This service does NOT "
        "use LLMs to calculate or alter indicators, weights, or MOS — LLM use "
        "is limited to product identification (candidate text only) and "
        "explaining already-computed results."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# The browser frontend (marketmind-ai) calls this API directly, so its origin
# must be allowed. Origins are configurable via CORS_ALLOW_ORIGINS (see config).
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router, tags=["Health"])
app.include_router(coverage_router, tags=["Coverage"])
app.include_router(catalog_router, tags=["Catalog"])
app.include_router(indicators_router, tags=["Indicators"])
app.include_router(mos_router, tags=["MOS"])
app.include_router(analyze_router, tags=["Analyze"])
app.include_router(admin_router)
