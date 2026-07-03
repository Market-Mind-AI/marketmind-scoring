from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.main import app


def test_refresh_weights_route() -> None:
    mock_service = AsyncMock()
    mock_service.calibrate.return_value = {"demand": 0.4567, "digital_readiness": 0.09}

    with patch("app.api.routes.admin.WeightCalibrationService", return_value=mock_service):
        client = TestClient(app)
        response = client.post("/admin/mos/refresh-weights", params={"period_key": "2026-07"})

    assert response.status_code == 200
    assert response.json() == {"weights": {"demand": 0.4567, "digital_readiness": 0.09}}
