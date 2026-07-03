import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.enrichment_service import EnrichmentService


def _mock_engine(trend_request_row: tuple[uuid.UUID] | None, suggestion_rows: list[tuple[str, str, str]]) -> MagicMock:
    engine = MagicMock()
    connection = AsyncMock()

    trend_request_result = MagicMock()
    trend_request_result.first.return_value = trend_request_row
    suggestions_result = MagicMock()
    suggestions_result.all.return_value = suggestion_rows
    connection.execute = AsyncMock(side_effect=[trend_request_result, suggestions_result])

    @asynccontextmanager
    async def _connect() -> AsyncGenerator[AsyncMock, None]:
        yield connection

    engine.connect = _connect
    return engine


async def test_best_suggestion_prefers_exact_title_match() -> None:
    run_id = uuid.uuid4()
    trend_request_id = uuid.uuid4()
    rows = [
        ("Air Fryer XL", "product", "mid-1"),
        ("air fryer", "product", "mid-2"),  # exact match (case-insensitive) to canonical_title
    ]
    service = EnrichmentService.__new__(EnrichmentService)  # bypass __init__ (no session needed for this method)
    with patch(
        "app.services.enrichment_service.get_external_engine",
        return_value=_mock_engine((trend_request_id,), rows),
    ):
        result = await service._best_suggestion(run_id, "air fryer")  # noqa: SLF001

    assert result == ("air fryer", "product", "mid-2")


async def test_best_suggestion_falls_back_to_first_when_no_exact_match() -> None:
    run_id = uuid.uuid4()
    trend_request_id = uuid.uuid4()
    rows = [("Air Fryer XL", "product", "mid-1"), ("Air Fryer Mini", "product", "mid-2")]
    service = EnrichmentService.__new__(EnrichmentService)
    with patch(
        "app.services.enrichment_service.get_external_engine",
        return_value=_mock_engine((trend_request_id,), rows),
    ):
        result = await service._best_suggestion(run_id, "air fryer")  # noqa: SLF001

    assert result == ("Air Fryer XL", "product", "mid-1")


async def test_best_suggestion_returns_none_when_no_candidates() -> None:
    run_id = uuid.uuid4()
    trend_request_id = uuid.uuid4()
    service = EnrichmentService.__new__(EnrichmentService)
    with patch(
        "app.services.enrichment_service.get_external_engine",
        return_value=_mock_engine((trend_request_id,), []),
    ):
        result = await service._best_suggestion(run_id, "air fryer")  # noqa: SLF001

    assert result is None
