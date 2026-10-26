"""Uma análise terminada não muda mais de estado."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.repositories.analysis_repository import AnalysisRepository


def _analise(status: str) -> SimpleNamespace:
    return SimpleNamespace(status=status, error_message=None, progress="x", finished_at=None)


async def test_a_done_analysis_never_becomes_failed() -> None:
    repo = AnalysisRepository(AsyncMock())
    analise = _analise("done")

    assert await repo.mark_failed(analise, error_message="narrativa falhou") is False
    assert (analise.status, analise.error_message) == ("done", None)


async def test_a_failed_analysis_keeps_its_first_reason_and_is_never_completed() -> None:
    repo = AnalysisRepository(AsyncMock())
    analise = _analise("failed")
    analise.error_message = "fonte fora do ar por 24h"

    assert await repo.mark_failed(analise, error_message="outra") is False
    assert await repo.mark_done(analise, result=[{"country": "ESP"}]) is False
    assert (analise.status, analise.error_message) == ("failed", "fonte fora do ar por 24h")


async def test_a_finished_analysis_is_not_put_back_to_running() -> None:
    repo = AnalysisRepository(AsyncMock())
    analise = _analise("done")

    assert await repo.mark_running(analise) is False
    assert analise.status == "done"


async def test_the_deadline_is_stored_once_and_never_pushed_forward() -> None:
    """Lido pela varredura de retomada e pelo sentinela, e nunca escrito. Uma
    retomada manda o prazo de novo e não pode adiá-lo."""
    from datetime import UTC, datetime

    repo = AnalysisRepository(AsyncMock())
    analise = SimpleNamespace(deadline_at=None)

    await repo.set_deadline(analise, datetime(2026, 10, 27, 12))
    await repo.set_deadline(analise, datetime(2026, 10, 28, 12, tzinfo=UTC))

    assert analise.deadline_at == datetime(2026, 10, 27, 12, tzinfo=UTC)
