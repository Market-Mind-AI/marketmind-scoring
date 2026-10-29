import asyncio
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.db.models  # noqa: F401 — registers every model on Base.metadata before _PRESERVE_TABLES logic reads it
from app import prompts
from app.services.indicators import pytrends as pytrends_indicators
from app.core.config import settings
from app.db.base import Base
from app.db.session import engine as app_engine
from app.main import app

# SAFETY GUARD (2026-07-14): this suite TRUNCATES tables (see
# _reset_database_after_test) and would otherwise wipe/pollute whatever DB it
# points at. Running it against the live/dev DB (marketmind_core) truncated
# coverage_index and self-widened normalization_reference into garbage more
# than once. Refuse to run unless pointed at a dedicated TEST database:
#   DATABASE_NAME=marketmind_test pytest
# Create/migrate it once with:
#   DATABASE_NAME=marketmind_test alembic upgrade head
if "test" not in settings.database_name.lower():
    raise RuntimeError(
        f"Refusing to run the test suite against non-test database '{settings.database_name}'. "
        "The suite truncates tables. Run with DATABASE_NAME=marketmind_test (see conftest.py)."
    )

# Keep the developer's Langfuse keys out of the tests: with them, the app's
# lifespan builds a real client pointed at Langfuse Cloud, and any test that
# fakes below the resolver would export its spans there. Tests that need
# tracing patch settings or replace observability.client themselves.
settings.langfuse_public_key = ""
settings.langfuse_secret_key = ""
# Same for Voyage: with the key, every resolve that reaches step 3 would embed
# for real. Tests that exercise semantic search inject their own client.
settings.voyage_api_key = ""

# Reference/seed data that migrations own and tests must never wipe:
#  - countries: migration 003's ~27-country basket.
#  - normalization_reference: the fixed absolute bounds seeded by migrations
#    005/010 (period_key="FIXED"). Truncating it silently broke the dev DB
#    when the suite ran against the same Postgres the container uses
#    (localhost:5436) — growth_score/demand_stability_score lost their bounds
#    and stopped normalizing (found 2026-07-09 during the live /analyze test).
#    Tests that write their own reference rows use unique uuid-suffixed codes,
#    so preserving the table is safe.
# Every other table accumulates real rows from tests that hit a real Postgres
# session (db_session fixture, or a route test's real get_session dependency)
# and must be wiped between tests, or later tests silently see earlier tests'
# rows (discovered 2026-07-08: product_catalog grew to 767 rows across runs,
# making embedding/trigram "no match expected" tests flaky — see memory
# marketmind_known_gaps item 14).
#  - metric_explanations / metric_band_phrases: the explanation content seeded
#    by migration 016 (the editable per-score/per-band phrases the engines read
#    to build each score's `explanation`). Truncating them would leave the
#    catalog empty, so every explanation would come back None after the first
#    test. Like normalization_reference, they are migration-owned reference data.
_PRESERVE_TABLES = {
    "countries",
    "normalization_reference",
    "metric_explanations",
    "metric_band_phrases",
}


@pytest.fixture(scope="session")
def event_loop() -> Generator[asyncio.AbstractEventLoop, None, None]:
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(autouse=True)
async def _reset_database_after_test() -> AsyncGenerator[None, None]:
    """Two unrelated problems, one fixture:

    1. No test here runs inside a rolled-back transaction, so any test that
       commits real rows (via db_session, or a route hitting the real
       get_session) leaves them behind for every later test. Truncating
       every mapped table except the seeded `countries` reference data
       after each test keeps them isolated without touching how any
       individual test/fixture is written. Uses a throwaway engine, never
       app_engine — see point 2.
    2. Each `TestClient(app)` instantiation (test_catalog_routes.py,
       test_mos_routes.py, ...) spins up its own anyio event loop/portal, but
       app/db/session.py's `engine` (used by the real `get_session`
       dependency those routes hit) is a single process-wide pool. A
       connection checked out under one TestClient's loop is invalid once
       that loop closes; disposing the pool after every test forces the
       next TestClient to open fresh connections under its own loop —
       otherwise reuse raises "Future ... attached to a different loop".
       That same stale-connection close can itself raise while dispose()
       gracefully terminates it (the loop it belonged to is already gone),
       which would otherwise fail the *next* test in teardown even though
       nothing about that test is wrong — swallowed here on purpose since
       the socket is already unusable either way.
    """
    yield
    table_names = [name for name in Base.metadata.tables if name not in _PRESERVE_TABLES]
    if table_names:
        reset_engine = create_async_engine(settings.database_url)
        try:
            async with reset_engine.begin() as connection:
                await connection.execute(text(f"TRUNCATE {', '.join(table_names)} RESTART IDENTITY CASCADE"))
        finally:
            await reset_engine.dispose()
    try:
        await app_engine.dispose()
    except RuntimeError:
        pass


@pytest.fixture(autouse=True)
def _forget_demand_rows() -> Generator[None, None, None]:
    """The demand rows are cached per (location, terms) for the length of an
    analysis. Tests reuse the same terms and location with different stored
    data, so each one starts with nothing remembered."""
    pytrends_indicators.forget_demand_rows()
    yield
    pytrends_indicators.forget_demand_rows()


@pytest.fixture(autouse=True)
def _reset_prompts_state() -> Generator[None, None, None]:
    """`app/prompts.py` keeps two module-level globals across calls: the
    linked-prompt ContextVar and the `_known` set (see F1, the known-set
    design that keeps Langfuse off the request path for unconfirmed names).
    Left dirty, one test's fetch/warm can make a later, unrelated test see a
    name as already known (skipping its own fetch) or inherit a stale link.
    """
    prompts.take_linked_prompt()
    prompts._known.clear()
    yield
    prompts.take_linked_prompt()
    prompts._known.clear()


@pytest.fixture
def mock_session() -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.execute = AsyncMock()
    session.add = MagicMock()
    session.add_all = MagicMock()
    return session


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Real session against the Core's own Postgres — used by the
    coverage/job dedup tests, which exercise ``FOR UPDATE SKIP LOCKED`` and
    a partial unique index that a mocked session can't meaningfully
    validate. Requires a running Postgres reachable via the standard
    DATABASE_* settings (see docker-compose.yml `postgres` service) with
    migrations applied (`alembic upgrade head`)."""
    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()
