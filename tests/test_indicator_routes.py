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
        response = client.post("/indicators/compute", json={"country": "USA", "period_key": "2026-10"})

    assert response.status_code == 200
    body = response.json()
    assert body["results"][0]["indicator_code"] == "gdp_per_capita_score"
    assert body["results"][0]["normalized_score"] == "75.5"


def test_compute_indicators_route_rejects_invalid_country_length() -> None:
    client = TestClient(app)
    response = client.post("/indicators/compute", json={"country": "US"})
    assert response.status_code == 422


async def test_the_demand_ceiling_reaches_the_engine() -> None:
    """It used to be dropped. The field was absent from the request schema, so
    Pydantic discarded it in silence and Demand Volume fell back to a fixed
    2,000,000 bound — 182,540 monthly searches scoring 9 instead of 34, with
    the macro dimensions left to decide the ranking on their own.

    The ruler is also the most expensive thing the system computes: collecting
    the reference markets is its slowest collection. It was being computed,
    carried, reported in the evidence, and then thrown away."""
    mock_engine = AsyncMock()
    mock_engine.compute_all.return_value = []

    with patch("app.api.routes.indicators.IndicatorEngine", return_value=mock_engine):
        response = TestClient(app).post(
            "/indicators/compute",
            json={
                "country": "BRA",
                "entity_key": str(uuid.uuid4()),
                "period_key": "2026-10",
                "demand_ceiling": 535040.0,
            },
        )

    assert response.status_code == 200
    assert mock_engine.compute_all.await_args.kwargs["demand_ceiling"] == 535040.0


async def test_a_ceiling_of_zero_is_refused() -> None:
    """Dividing by it would make every score infinite."""
    response = TestClient(app).post(
        "/indicators/compute", json={"country": "BRA", "demand_ceiling": 0}
    )

    assert response.status_code == 422
