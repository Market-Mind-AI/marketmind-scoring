"""Read-only connections to the 4 collection microservices' databases.

The Core does not own these schemas and must not run migrations against them or
map full ORM models for their tables — each service's internal schema evolves
independently. Reads happen via plain SQLAlchemy Core (``text()`` / reflected
tables), scoped to a read-only role created with ``scripts/create_readonly_roles.sql``.

See README "Conectividade entre serviços" for why explicit per-source host/port
settings are used instead of a shared Docker network.
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.core.config import settings
from app.domain.enums import DataSource

_ENGINES: dict[DataSource, AsyncEngine] = {
    DataSource.PYTRENDS: create_async_engine(settings.pytrends_database_url, echo=False, pool_pre_ping=True),
    DataSource.WORLDBANK: create_async_engine(settings.worldbank_database_url, echo=False, pool_pre_ping=True),
    DataSource.OECD: create_async_engine(settings.oecd_database_url, echo=False, pool_pre_ping=True),
    DataSource.UNDATA: create_async_engine(settings.undata_database_url, echo=False, pool_pre_ping=True),
}


def get_external_engine(source: DataSource) -> AsyncEngine:
    return _ENGINES[source]


async def get_external_connection(source: DataSource) -> AsyncGenerator[AsyncConnection, None]:
    engine = get_external_engine(source)
    async with engine.connect() as connection:
        yield connection


async def dispose_all_external_engines() -> None:
    for engine in _ENGINES.values():
        await engine.dispose()
