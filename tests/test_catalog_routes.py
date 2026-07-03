import uuid
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.api.routes.catalog import get_catalog_service
from app.main import app
from app.schemas.catalog import CatalogResolveResponse


def test_resolve_route() -> None:
    mock_service = AsyncMock()
    entity_key = uuid.uuid4()
    mock_service.resolve.return_value = CatalogResolveResponse(
        entity_key=entity_key, canonical_title="Air Fryer", method="created"
    )
    app.dependency_overrides[get_catalog_service] = lambda: mock_service
    try:
        client = TestClient(app)
        response = client.post("/catalog/resolve", json={"text": "Air Fryer", "source": "test"})
        assert response.status_code == 200
        body = response.json()
        assert body["entity_key"] == str(entity_key)
        assert body["method"] == "created"
    finally:
        app.dependency_overrides.clear()


def test_resolve_route_rejects_empty_text() -> None:
    client = TestClient(app)
    response = client.post("/catalog/resolve", json={"text": "", "source": "test"})
    assert response.status_code == 422


def test_get_entity_route_returns_404_for_unknown_entity() -> None:
    client = TestClient(app)
    response = client.get(f"/catalog/{uuid.uuid4()}")
    assert response.status_code == 404
