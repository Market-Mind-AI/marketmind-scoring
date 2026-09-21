"""The live stream, and what the waiting screen learns from it.

The run happens in the orchestrator. This endpoint watches the row and pushes,
so the screen is told rather than having to ask.
"""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.main import app


def _row(status: str, *, progress: str | None = None, explanation: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(status=status, progress=progress, explanation=explanation)


def _events(rows: list[SimpleNamespace | None]) -> list[dict]:
    """Drive the stream through a scripted sequence of database states."""
    repo = AsyncMock()
    repo.get = AsyncMock(side_effect=rows)
    request_id = str(uuid.uuid4())

    with (
        patch("app.api.routes.analyze.AnalysisRepository", return_value=repo),
        patch("app.api.routes.analyze._SSE_POLL_SECONDS", 0),
    ):
        with TestClient(app) as client:
            with client.stream("GET", f"/analyze/{request_id}/stream") as response:
                assert response.status_code == 200
                # Never cached: a cached stream is a screen that never moves.
                assert response.headers["cache-control"] == "no-store"
                body = "".join(response.iter_text())

    return [json.loads(line[len("data: ") :]) for line in body.splitlines() if line.startswith("data: ")]


def test_the_progress_line_is_pushed_as_it_moves() -> None:
    """Within one status. Watching only the status meant the caller had to poll
    for the progress line anyway, which is what this endpoint exists to avoid."""
    events = _events(
        [
            _row("running", progress="collecting Spain"),
            _row("running", progress="collecting Portugal"),
            _row("running", progress="scoring the markets"),
            _row("done", progress=None, explanation="Spain leads."),
        ]
    )

    assert [e["progress"] for e in events] == [
        "collecting Spain",
        "collecting Portugal",
        "scoring the markets",
        None,
    ]
    assert events[-1]["status"] == "done"


def test_the_stream_stays_open_until_the_narrative_lands() -> None:
    """The wait people actually notice. The scores are done and the answer is
    already in the database, while the screen still says it is being written —
    because the stream used to close here and leave the caller polling for the
    slowest part of the run."""
    events = _events(
        [
            _row("running", progress="scoring the markets"),
            _row("done", progress=None, explanation=None),  # scores in, text not yet
            _row("done", progress=None, explanation=None),
            _row("done", progress=None, explanation="Spain leads."),
        ]
    )

    assert [e["has_explanation"] for e in events] == [False, False, True]
    assert events[-1]["status"] == "done"


def test_a_narrative_that_never_arrives_closes_the_stream_saying_so() -> None:
    """The narration is best-effort and the scores are already delivered, so a
    text that never lands must end the stream rather than hold it open — and it
    has to say which of the two happened."""
    rows = [_row("running", progress="scoring")] + [_row("done", explanation=None)] * 8

    with patch("app.api.routes.analyze._SSE_NARRATIVE_SECONDS", 0):
        events = _events(rows)

    assert events[-1]["narrative_abandoned"] is True
    assert events[-1]["has_explanation"] is False


def test_a_failed_run_closes_immediately() -> None:
    events = _events([_row("running", progress="collecting"), _row("failed", progress=None)])

    assert events[-1]["status"] == "failed"
    assert len(events) == 2


def test_a_request_that_does_not_exist_says_so_and_closes() -> None:
    events = _events([None])

    assert events == [{"request_id": events[0]["request_id"], "status": "not_found"}]


def test_nothing_is_pushed_while_nothing_changes() -> None:
    """Otherwise the screen redraws every 1.5 seconds for no reason, and a real
    change is indistinguishable from the noise."""
    same = _row("running", progress="collecting Spain")
    events = _events([same, same, same, _row("done", explanation="Spain leads.")])

    assert len(events) == 2
