"""What one request spent on models.

The orchestrator is what decides; this service is where the model calls that
decision causes actually happen. So the tokens are spent here and have to be
reported back, or the only place able to say what a run cost is the one place
that cannot see it.

A context variable rather than a parameter threaded through every call site:
the providers are pure transport (see base.py) and the routes do not know which
of them ran, so adding a return value would have changed every signature between
the two for a number neither of them uses. The middleware in main.py opens one
tally per HTTP request and puts the totals on the response headers; the
orchestrator adds them up across the run.

Counting never raises. A provider whose response omits usage is counted as a
call with unknown tokens, which is visible in the call count, rather than
silently as zero.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class Usage:
    """One request's model spend."""

    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    # Calls whose provider gave no usage figures. Kept apart so a total of zero
    # tokens over three calls reads as "not reported" and not as "free".
    calls_without_usage: int = 0
    models: list[str] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


_current: ContextVar[Usage | None] = ContextVar("llm_usage", default=None)


@contextmanager
def collect() -> Iterator[Usage]:
    """Tally the model spend of everything that runs inside this block."""
    usage = Usage()
    token = _current.set(usage)
    try:
        yield usage
    finally:
        _current.reset(token)


def current() -> Usage | None:
    """The tally open for this request, or None outside one."""
    return _current.get()


def record(model: str, raw_usage: Any) -> None:
    """Add one completed model call to the current tally, if one is open.

    ``raw_usage`` is the provider's own usage object — Anthropic's
    ``input_tokens``/``output_tokens`` or OpenAI's ``prompt_tokens``/
    ``completion_tokens``. Both shapes are read; anything else counts as a call
    with unknown tokens.
    """
    usage = _current.get()
    if usage is None:
        return
    usage.calls += 1
    if model and model not in usage.models:
        usage.models.append(model)

    entrada = _first_int(raw_usage, "input_tokens", "prompt_tokens")
    saida = _first_int(raw_usage, "output_tokens", "completion_tokens")
    if entrada is None and saida is None:
        usage.calls_without_usage += 1
        return
    usage.input_tokens += entrada or 0
    usage.output_tokens += saida or 0


def _first_int(raw_usage: Any, *names: str) -> int | None:
    for name in names:
        value = getattr(raw_usage, name, None)
        if value is None and isinstance(raw_usage, dict):
            value = raw_usage.get(name)
        if isinstance(value, int):
            return value
    return None
