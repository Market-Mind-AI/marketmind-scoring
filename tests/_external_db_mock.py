"""Shared helper for mocking app.db.external.get_external_engine — the raw
indicator functions (app/services/indicators/*.py) read from the 4
sibling services' own databases, which aren't part of this test
environment (same reasoning as test_enrichment_service.py / test_health.py:
mock the connection, not the network/DB)."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock


def result_with_all(rows: list[tuple[Any, ...]]) -> MagicMock:
    result = MagicMock()
    result.all.return_value = rows
    return result


def result_with_first(row: tuple[Any, ...] | None) -> MagicMock:
    result = MagicMock()
    result.first.return_value = row
    return result


def make_mock_engine(execute_results: list[MagicMock]) -> MagicMock:
    """execute_results supplies one mocked Result per connection.execute()
    call, in the exact order the code under test makes them."""
    engine = MagicMock()
    connection = AsyncMock()
    connection.execute = AsyncMock(side_effect=execute_results)

    @asynccontextmanager
    async def _connect() -> AsyncGenerator[AsyncMock, None]:
        yield connection

    engine.connect = _connect
    return engine
