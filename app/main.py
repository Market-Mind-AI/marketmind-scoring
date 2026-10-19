import asyncio
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from app import observability, pricing, prompts
from app.api.routes.admin import router as admin_router
from app.api.routes.analyze import router as analyze_router
from app.api.routes.catalog import router as catalog_router
from app.api.routes.countries import router as countries_router
from app.api.routes.coverage import router as coverage_router
from app.api.routes.health import router as health_router
from app.api.routes.indicators import router as indicators_router
from app.api.routes.internal import router as internal_router
from app.api.routes.mos import router as mos_router
from app.core.config import settings
from app.core.logging import logger
from app.db.session import engine
from app.services import demand_spend
from app.services.analysis_resume_service import AnalysisResumeService
from app.services.llm import usage as llm_usage


async def _manter_prompts_aquecidos(registry: Mapping[str, str]) -> None:
    """Keep confirming unconfirmed prompt names, forever, every 60 s.

    get_prompt is a synchronous HTTP call; run from a thread so it never blocks
    the event loop. A name only pays this once — `warm` skips whatever is
    already confirmed — so a seeded or recovered prompt gets off the request
    path within about 60 s of becoming available, and steady state (every
    name already confirmed) makes no request at all.
    """
    while True:
        await asyncio.to_thread(prompts.warm, registry)
        if not pricing.loaded():
            # The model price table, read once from Langfuse (app/pricing.py)
            # and retried here until it answers, so a Langfuse that was down
            # at startup does not leave every model call unpriced for the
            # life of the process.
            await asyncio.to_thread(pricing.load)
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info(f"Starting {settings.app_name} in {settings.app_env} mode")
    if not observability.tracing_enabled():
        logger.warning("Langfuse is not configured; model calls will not be traced.")
    # A name Langfuse has never confirmed is never fetched inline (see
    # app/prompts.py's `_known`), so nothing on the request path can block on
    # a cold or failed fetch. This loop is what confirms names, off the event
    # loop, so a request never has to. Not awaited: startup never waits on
    # Langfuse, and it keeps running for the life of the process so a prompt
    # seeded or recovered after startup is still picked up, without a deploy.
    aquecimento = None
    if observability.tracing_enabled():
        from app.prompt_registry import PROMPTS

        aquecimento = asyncio.create_task(_manter_prompts_aquecidos(PROMPTS))
    # Análise que morreu com o processo anterior fica em `running` para sempre,
    # e com a regra de "nada parcial sai" (D3) isso deixou de ser um registro
    # feio para virar um pedido pago que nunca entrega. A varredura retoma o que
    # está calado e encerra o que passou do prazo — ver analysis_resume_service.
    varredura = asyncio.create_task(AnalysisResumeService().run_forever())
    yield
    varredura.cancel()
    with suppress(asyncio.CancelledError):
        await varredura
    if aquecimento is not None:
        aquecimento.cancel()
        with suppress(asyncio.CancelledError):
            await aquecimento
    await engine.dispose()
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
    # Set when a proxy serves this API under a prefix and strips it before
    # forwarding. On Azure the ingress publishes the Core at /api and rewrites
    # it away, so without this FastAPI believes it is at the root and builds the
    # Swagger page pointing at /openapi.json — an absolute path that leaves the
    # /api route entirely, lands on the frontend's ingress, and 404s. The API
    # itself works either way; it is only the generated docs URLs that are wrong,
    # which is why it looks like a broken Swagger rather than a routing problem.
    #
    # Empty by default so the local docker-compose setup, where nothing strips a
    # prefix, is unaffected.
    root_path=settings.root_path,
)


@app.middleware("http")
async def report_llm_usage(request: Request, call_next) -> Response:
    """Say, on every response, what that request spent: tokens on models, and
    dollars at DataForSEO for the demand data it had collected.

    The orchestrator decides; the model calls that decision causes happen here.
    Without this the only component able to report what a run cost is the one
    component that cannot see it.

    Headers rather than a field on each response body: it covers every endpoint
    at once, including the ones whose response shape is fixed by the method, and
    a caller that does not read them is unaffected.
    """
    with llm_usage.collect() as spend, demand_spend.collect() as demand:
        response = await call_next(request)
    # What the Google Ads collector was billed by DataForSEO for the
    # collections this request dispatched, relayed under the collector's own
    # header names (see demand_spend). The one cost that is not a model.
    for name, value in demand_spend.headers(demand).items():
        response.headers[name] = value
    if spend.calls:
        response.headers["X-LLM-Input-Tokens"] = str(spend.input_tokens)
        response.headers["X-LLM-Output-Tokens"] = str(spend.output_tokens)
        response.headers["X-LLM-Calls"] = str(spend.calls)
        if spend.cache_read_tokens or spend.cache_write_tokens:
            # Parts of X-LLM-Input-Tokens, not additions: how much of the
            # prompt came from the cache and how much was written to it.
            response.headers["X-LLM-Cache-Read-Tokens"] = str(spend.cache_read_tokens)
            response.headers["X-LLM-Cache-Write-Tokens"] = str(spend.cache_write_tokens)
        if spend.calls_without_usage:
            # So a total of zero over three calls reads as "not reported by the
            # provider" rather than as "free".
            response.headers["X-LLM-Calls-Without-Usage"] = str(spend.calls_without_usage)
        # In dollars, by the price table Langfuse holds (app/pricing.py), so
        # the orchestrator can say what the whole run cost without waiting
        # for Langfuse to ingest the trace. By model too, for the breakdown.
        response.headers["X-LLM-Cost-USD"] = f"{spend.cost_usd:.6f}"
        if spend.cost_by_model:
            response.headers["X-LLM-Cost-By-Model"] = ",".join(
                f"{model}={usd:.6f}" for model, usd in sorted(spend.cost_by_model.items())
            )
        if spend.calls_without_cost:
            response.headers["X-LLM-Calls-Without-Cost"] = str(spend.calls_without_cost)
    return response


@app.middleware("http")
async def adopt_request_trace(request: Request, call_next) -> Response:
    """Make the caller's trace the current one for this request.

    The orchestrator sends its run's W3C `traceparent`; adopting it puts every
    model call this request makes inside that run's trace, under its `analysis`
    span. A request without one — the browser's identify-image — starts its own.
    """
    with observability.adopt_trace_context(request.headers):
        return await call_next(request)


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
app.include_router(countries_router, tags=["Countries"])
app.include_router(indicators_router, tags=["Indicators"])
app.include_router(mos_router, tags=["MOS"])
app.include_router(analyze_router, tags=["Analyze"])
app.include_router(internal_router)
app.include_router(admin_router)
