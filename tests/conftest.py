import asyncio
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.db.models  # noqa: F401 — registers every model on Base.metadata before _PRESERVE_TABLES logic reads it
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
_PRESERVE_TABLES = {"countries", "normalization_reference"}


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
