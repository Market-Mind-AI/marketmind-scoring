import uuid
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.api.routes.coverage import get_coverage_service
from app.domain.enums import DataSource, JobTaskStatus
from app.main import app
from app.schemas.coverage import GapStatus, JobTaskResult

_GAP_PAYLOAD = {
    "source": "worldbank",
    "country": "USA",
    "indicator_code": "NY.GDP.PCAP.CD",
    "period_key": "2026",
    "collection_params": {"start_year": 2020, "end_year": 2026},
}


def test_check_coverage_route() -> None:
    mock_service = AsyncMock()
    mock_service.check_coverage.return_value = [
        GapStatus(
            source=DataSource.WORLDBANK,
            entity_key=None,
            country="USA",
            indicator_code="NY.GDP.PCAP.CD",
            period_key="2026",
            covered=False,
        )
    ]
    app.dependency_overrides[get_coverage_service] = lambda: mock_service
    try:
        client = TestClient(app)
        response = client.post("/coverage/check", json={"gaps": [_GAP_PAYLOAD]})
        assert response.status_code == 200
        body = response.json()
        assert body["results"][0]["covered"] is False
    finally:
        app.dependency_overrides.clear()


def test_create_job_route() -> None:
    mock_service = AsyncMock()
    task_id = uuid.uuid4()
    job_id = uuid.uuid4()
    mock_service.check_or_collect.return_value = (
        job_id,
        [
            JobTaskResult(
                id=task_id,
                source=DataSource.WORLDBANK,
                entity_key=None,
                country="USA",
                indicator_code="NY.GDP.PCAP.CD",
                period_key="2026",
                status=JobTaskStatus.DONE,
                ingestion_run_id=uuid.uuid4(),
                error_message=None,
            )
        ],
    )
    app.dependency_overrides[get_coverage_service] = lambda: mock_service
    try:
        client = TestClient(app)
        response = client.post("/jobs", json={"requested_by": "test", "gaps": [_GAP_PAYLOAD]})
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == str(job_id)
        assert body["status"] == "done"
        assert body["tasks"][0]["id"] == str(task_id)
    finally:
        app.dependency_overrides.clear()


def test_check_coverage_rejects_empty_gaps() -> None:
    client = TestClient(app)
    response = client.post("/coverage/check", json={"gaps": []})
    assert response.status_code == 422
