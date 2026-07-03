import uuid
from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.db.models.indicator_value import IndicatorValue
from app.main import app


def test_compute_indicators_route() -> None:
    row = IndicatorValue(
        id=uuid.uuid4(), entity_key=None, country="USA", indicator_code="gdp_per_capita_score", period_type="month",
        period_key="2026-07", source="worldbank", raw_value=Decimal("50000"), normalized_score=Decimal("75.5"),
        direction="direct", calculation_window="y_latest", availability_status="available", notes=None,
        computed_at=datetime(2026, 7, 2),
    )  # fmt: skip

    mock_engine = AsyncMock()
    mock_engine.compute_all.return_value = [row]

    with patch("app.api.routes.indicators.IndicatorEngine", return_value=mock_engine):
        client = TestClient(app)
        response = client.post("/indicators/compute", json={"country": "USA"})

    assert response.status_code == 200
    body = response.json()
    assert body["results"][0]["indicator_code"] == "gdp_per_capita_score"
    assert body["results"][0]["normalized_score"] == "75.5"


def test_compute_indicators_route_rejects_invalid_country_length() -> None:
    client = TestClient(app)
    response = client.post("/indicators/compute", json={"country": "US"})
    assert response.status_code == 422
