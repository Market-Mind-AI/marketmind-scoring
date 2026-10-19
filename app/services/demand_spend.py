"""What one request spent at DataForSEO, relayed from the Google Ads collector.

The collector is billed per task and reports the amount on its response headers
(its app/core/spend.py). This service is the one that causes those tasks — one
per gap it dispatches — and the orchestrator is the one that adds up a run. So
the figure has to pass through here: the collection client adds each dispatch's
headers to this tally, and the middleware in main.py puts the request's total on
the response, under the same header names, next to the model tokens.

Counting never raises. A collector response without the headers adds nothing:
the three other sources (World Bank, OECD, UN Data) cost nothing per call and
say nothing.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

COST_HEADER = "X-DataForSEO-Cost-USD"
CALLS_HEADER = "X-DataForSEO-Calls"
CALLS_WITHOUT_COST_HEADER = "X-DataForSEO-Calls-Without-Cost"


@dataclass
class Spend:
    usd: float = 0.0
    calls: int = 0
    # Tasks the collector said it ran but could not price. Kept apart so a
    # total of zero over three calls reads as "not reported", not as "free".
    calls_without_cost: int = 0


_current: ContextVar[Spend | None] = ContextVar("demand_spend", default=None)


@contextmanager
def collect() -> Iterator[Spend]:
    """Tally the DataForSEO spend of everything that runs inside this block."""
    spend = Spend()
    token = _current.set(spend)
    try:
        yield spend
    finally:
        _current.reset(token)


def current() -> Spend | None:
    """The tally open for this request, or None outside one."""
    return _current.get()


def read(headers: Mapping[str, str]) -> Spend | None:
    """One collector response's reported spend, or None when it reports none."""
    if CALLS_HEADER not in headers:
        return None
    return Spend(
        usd=_as_float(headers.get(COST_HEADER)),
        calls=_as_int(headers.get(CALLS_HEADER)),
        calls_without_cost=_as_int(headers.get(CALLS_WITHOUT_COST_HEADER)),
    )


def add(reported: Spend | None) -> None:
    """Add one collector response's reported spend to the current tally."""
    spend = _current.get()
    if spend is None or reported is None:
        return
    spend.usd += reported.usd
    spend.calls += reported.calls
    spend.calls_without_cost += reported.calls_without_cost


def headers(spend: Spend) -> dict[str, str]:
    """The tally as response headers; empty when no task was run."""
    if not spend.calls:
        return {}
    out = {COST_HEADER: f"{spend.usd:.6f}", CALLS_HEADER: str(spend.calls)}
    if spend.calls_without_cost:
        out[CALLS_WITHOUT_COST_HEADER] = str(spend.calls_without_cost)
    return out


def _as_int(value: str | None) -> int:
    try:
        return int(value) if value is not None else 0
    except ValueError:
        return 0


def _as_float(value: str | None) -> float:
    try:
        return float(value) if value is not None else 0.0
    except ValueError:
        return 0.0
