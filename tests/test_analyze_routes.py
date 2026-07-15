import uuid
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.main import app
from app.schemas.catalog import CatalogResolveResponse, IdentifyImageResponse


def test_identify_image_route() -> None:
    entity_key = uuid.uuid4()
    mock_service = AsyncMock()
    mock_service.identify.return_value = IdentifyImageResponse(
        entity_key=entity_key,
        canonical_title="Philips air fryer",
        method="created",
        product_name="air fryer",
        brand="Philips",
        category="kitchen",
        target_age_min=None,
        target_age_max=None,
        estimated_price=89.9,
        price_currency="USD",
        confidence=0.9,
    )

    from app.api.routes.catalog import get_product_identification_service

    app.dependency_overrides[get_product_identification_service] = lambda: mock_service
    try:
        client = TestClient(app)
        response = client.post(
            "/catalog/identify-image",
            json={"image_base64": "ZmFrZQ==", "image_media_type": "image/jpeg", "countries": ["USA"]},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["entity_key"] == str(entity_key)
        assert body["brand"] == "Philips"
        assert body["estimated_price"] == 89.9
    finally:
        app.dependency_overrides.clear()


def test_identify_image_route_rejects_unsupported_media_type() -> None:
    client = TestClient(app)
    response = client.post(
        "/catalog/identify-image",
        json={"image_base64": "ZmFrZQ==", "image_media_type": "image/bmp"},
    )
    assert response.status_code == 422


def test_analyze_route_returns_202_with_request_id() -> None:
    entity_key = uuid.uuid4()
    mock_catalog_service = AsyncMock()
    mock_catalog_service.resolve.return_value = CatalogResolveResponse(
        entity_key=entity_key, canonical_title="Philips air fryer", method="created"
    )

    fake_request_id = uuid.uuid4()
    mock_analysis_repo = AsyncMock()
    mock_analysis_repo.create.return_value = type("R", (), {"id": fake_request_id, "status": "pending"})()

    mock_analysis_service = AsyncMock()
    mock_analysis_service.run = AsyncMock()

    with (
        patch("app.api.routes.analyze.CatalogService", return_value=mock_catalog_service),
        patch("app.api.routes.analyze.AnalysisRepository", return_value=mock_analysis_repo),
        patch("app.api.routes.analyze.AnalysisService", return_value=mock_analysis_service),
    ):
        client = TestClient(app)
        response = client.post(
            "/analyze",
            json={"product_name": "air fryer", "brand": "Philips", "countries": ["USA"]},
        )

    assert response.status_code == 202
    body = response.json()
    assert body["request_id"] == str(fake_request_id)
    assert body["status"] == "pending"
    mock_analysis_service.run.assert_awaited_once_with(fake_request_id)


def test_analyze_route_rejects_empty_countries() -> None:
    client = TestClient(app)
    response = client.post("/analyze", json={"product_name": "air fryer", "countries": []})
    assert response.status_code == 422


def test_get_analysis_route_returns_404_for_unknown_request() -> None:
    client = TestClient(app)
    response = client.get(f"/analyze/{uuid.uuid4()}")
    assert response.status_code == 404


def test_get_analysis_route_rejects_non_uuid() -> None:
    client = TestClient(app)
    response = client.get("/analyze/not-a-uuid")
    assert response.status_code == 422
