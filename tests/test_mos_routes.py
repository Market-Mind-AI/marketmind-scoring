import uuid
from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.core.exceptions import EntityNotFoundError
from app.db.models.mos_result import MosResult
from app.main import app


def _mos_result(**overrides: object) -> MosResult:
    defaults: dict[str, object] = dict(
        id=uuid.uuid4(), entity_key=uuid.uuid4(), country="USA", period_type="month", period_key="2026-07",
        mos=Decimal("72.5"), mos_before_cap=Decimal("72.5"), capped_by=None, topsis_score=Decimal("0.725"),
        availability_status="available", excluded_reason=None, computed_at=datetime(2026, 7, 2),
    )  # fmt: skip
    defaults.update(overrides)
    return MosResult(**defaults)  # type: ignore[arg-type]


def test_compute_mos_route() -> None:
    mock_engine = AsyncMock()
    mock_engine.compute_mos.return_value = [_mos_result()]

    with patch("app.api.routes.mos.MosEngine", return_value=mock_engine):
        client = TestClient(app)
        response = client.post(
            "/mos/compute", json={"entity_key": str(uuid.uuid4()), "countries": ["USA"], "period_key": "2026-10"}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["results"][0]["country"] == "USA"
    assert body["results"][0]["mos"] == "72.5"


def test_compute_mos_route_rejects_empty_countries() -> None:
    client = TestClient(app)
    response = client.post("/mos/compute", json={"entity_key": str(uuid.uuid4()), "countries": []})
    assert response.status_code == 422


def test_compute_mos_route_returns_404_for_unknown_entity() -> None:
    mock_engine = AsyncMock()
    mock_engine.compute_mos.side_effect = EntityNotFoundError("not found")

    with patch("app.api.routes.mos.MosEngine", return_value=mock_engine):
        client = TestClient(app)
        response = client.post(
            "/mos/compute", json={"entity_key": str(uuid.uuid4()), "countries": ["USA"], "period_key": "2026-10"}
        )

    assert response.status_code == 404
