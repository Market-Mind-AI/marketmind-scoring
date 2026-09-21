from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from app.core.config import settings
from app.db.session import get_session
from app.main import app


async def _override_get_session() -> AsyncGenerator[AsyncMock, None]:
    session = AsyncMock()
    session.execute = AsyncMock()
    yield session


def _client() -> TestClient:
    # `Depends(get_session)` binds the callable at route-definition time, so
    # patching the module attribute afterward would not intercept it — the
    # correct way to swap a FastAPI dependency in tests is
    # `app.dependency_overrides`, not `unittest.mock.patch`.
    app.dependency_overrides[get_session] = _override_get_session
    return TestClient(app)


def test_health_endpoint_all_connected() -> None:
    reachable = AsyncMock(
        return_value=httpx.Response(200, json={"status": "ok"}, request=httpx.Request("GET", "http://x/health"))
    )
    try:
        with patch("httpx.AsyncClient.get", reachable):
            response = _client().get("/health")

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "ok"
            assert data["database"] == "connected"
            # Whatever this service is configured to be called, echoed back —
            # not a literal. APP_NAME comes from the environment, so asserting a
            # name here makes the test pass or fail on the developer's own .env.
            assert data["app_name"] == settings.app_name
            assert len(data["external_sources"]) == 4
            assert all(s["status"] == "connected" for s in data["external_sources"])
    finally:
        app.dependency_overrides.clear()


def test_health_endpoint_external_source_down() -> None:
    try:
        with patch("httpx.AsyncClient.get", AsyncMock(side_effect=httpx.ConnectError("refused"))):
            response = _client().get("/health")

            assert response.status_code == 200
            assert all(s["status"] == "disconnected" for s in response.json()["external_sources"])
    finally:
        app.dependency_overrides.clear()


def test_a_collection_service_that_answers_an_error_is_not_connected() -> None:
    """The probe used to open a SQL connection to each service's database, which
    reported "connected" for a reachable database behind an application that was
    down — and once for a database that was reachable but was the wrong one,
    because two services had their ports swapped."""
    broken = AsyncMock(
        return_value=httpx.Response(500, request=httpx.Request("GET", "http://x/health"))
    )
    try:
        with patch("httpx.AsyncClient.get", broken):
            response = _client().get("/health")

            assert all(s["status"] == "disconnected" for s in response.json()["external_sources"])
    finally:
        app.dependency_overrides.clear()


def test_healthz_answers_without_touching_anything() -> None:
    """What a liveness probe asks: can THIS process still answer. It must not
    depend on the database or on the four collection services — a probe that
    does restarts this service because a different one is unwell, and the
    restart kills whatever analysis this process was running.

    No dependency override and no patched httpx here, deliberately: if this
    endpoint ever starts needing either, this test fails.
    """
    response = TestClient(app).get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
