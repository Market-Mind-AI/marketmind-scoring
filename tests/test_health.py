from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.db.session import get_session
from app.main import app


def _mock_engine() -> MagicMock:
    engine = MagicMock()
    mock_connection = AsyncMock()
    mock_connection.execute = AsyncMock()

    @asynccontextmanager
    async def _connect() -> AsyncGenerator[AsyncMock, None]:
        yield mock_connection

    engine.connect = _connect
    return engine


async def _override_get_session() -> AsyncGenerator[AsyncMock, None]:
    session = AsyncMock()
    session.execute = AsyncMock()
    yield session


def test_health_endpoint_all_connected() -> None:
    # `Depends(get_session)` binds the callable at route-definition time, so
    # patching the module attribute afterward would not intercept it — the
    # correct way to swap a FastAPI dependency in tests is
    # `app.dependency_overrides`, not `unittest.mock.patch`.
    app.dependency_overrides[get_session] = _override_get_session
    try:
        with patch("app.api.routes.health.get_external_engine", return_value=_mock_engine()):
            client = TestClient(app)
            response = client.get("/health")

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "ok"
            assert data["database"] == "connected"
            assert data["app_name"] == "marketmind-core"
            assert len(data["external_sources"]) == 4
            assert all(s["status"] == "connected" for s in data["external_sources"])
    finally:
        app.dependency_overrides.clear()


def test_health_endpoint_external_source_down() -> None:
    def _raise_engine(_source: object) -> MagicMock:
        raise ConnectionError("could not connect")

    app.dependency_overrides[get_session] = _override_get_session
    try:
        with patch("app.api.routes.health.get_external_engine", side_effect=_raise_engine):
            client = TestClient(app)
            response = client.get("/health")

            assert response.status_code == 200
            data = response.json()
            assert all(s["status"] == "disconnected" for s in data["external_sources"])
    finally:
        app.dependency_overrides.clear()
