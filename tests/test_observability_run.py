"""The nightly prewarm is one trace, not one trace per collector call."""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from app import observability


class _FakeLangfuse:
    def __init__(self) -> None:
        self.span = MagicMock()
        self.entered = 0
        self.exited_with: list[type | None] = []

    def start_as_current_observation(self, **fields):
        fake = self

        @contextmanager
        def manager():
            fake.entered += 1
            try:
                yield fake.span
            except BaseException as exc:
                fake.exited_with.append(type(exc))
                raise
            else:
                fake.exited_with.append(None)

        return manager()


def test_without_tracing_it_does_nothing() -> None:
    with patch.object(observability, "client", return_value=None):
        with observability.run("prewarm-macro") as handle:
            handle.update(level="WARNING")


def test_the_run_is_opened_as_the_current_observation_and_closed() -> None:
    fake = _FakeLangfuse()
    with patch.object(observability, "client", return_value=fake):
        with observability.run("prewarm-macro") as handle:
            handle.update(level="WARNING", status_message="20 coleta(s) falharam")

    assert fake.entered == 1
    assert fake.exited_with == [None]
    fake.span.update.assert_called_with(level="WARNING", status_message="20 coleta(s) falharam")


def test_a_run_that_raises_is_marked_error_and_the_exception_passes() -> None:
    fake = _FakeLangfuse()
    with patch.object(observability, "client", return_value=fake):
        with pytest.raises(RuntimeError):
            with observability.run("prewarm-macro"):
                raise RuntimeError("boom")

    assert fake.exited_with == [RuntimeError]
    assert fake.span.update.call_args.kwargs["level"] == "ERROR"
