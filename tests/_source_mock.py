"""Stands in for the collection services' read APIs.

The raw-value functions (app/services/indicators/*.py) read what the four
collection services hold, over HTTP. Those services are not part of this test
environment, so the read functions are patched rather than the network.

Each helper takes the returns in the order the code under test asks for them,
so a test that reads two series still says plainly which answer comes first.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, patch


@contextmanager
def patch_source(module: str, name: str, *returns: Any) -> Iterator[AsyncMock]:
    """Patch one read function, answering each call with the next return."""
    mock = AsyncMock(side_effect=list(returns) if len(returns) > 1 else None)
    if len(returns) == 1:
        mock = AsyncMock(return_value=returns[0])
    with patch(f"{module}.{name}", mock):
        yield mock


@contextmanager
def patch_worldbank(*series: list[float]) -> Iterator[AsyncMock]:
    """Each call answers with one series of values, newest year first."""
    returns = [[(2024 - i, value) for i, value in enumerate(values)] for values in series]
    with patch_source("app.services.indicators.worldbank", "worldbank_series", *returns) as mock:
        yield mock


@contextmanager
def patch_oecd(values: list[float]) -> Iterator[AsyncMock]:
    """One deduplicated series, newest period first."""
    points = [(f"2026-Q{4 - (i % 4)}", value) for i, value in enumerate(values)]
    with patch_source("app.services.indicators.oecd", "oecd_series", points) as mock:
        yield mock


@contextmanager
def patch_undata(*series: list[tuple[str, float]]) -> Iterator[AsyncMock]:
    """Each call answers with one (time_period, value) series, newest first."""
    with patch_source("app.services.indicators.undata", "undata_series", *series) as mock:
        yield mock


@contextmanager
def patch_pytrends(
    approved: list[tuple[str, float, Any]],
    related: list[tuple[str, float, Any, Any, Any]] | None = None,
    *,
    missing: list[str] | None = None,
) -> Iterator[None]:
    """The stored search volume and related keywords for one country.

    `approved` rows are (keyword, volume, monthly_searches); `related` rows are
    (keyword, volume, monthly_searches, cpc, competition_index) -- the shapes
    the collection service serves.
    """
    volume = {
        "keywords": [
            {"keyword": keyword, "search_volume": vol, "monthly_searches": monthly}
            for keyword, vol, monthly in approved
        ],
        "missing": missing or [],
    }
    related_payload = [
        {
            "keyword": keyword,
            "search_volume": vol,
            "monthly_searches": monthly,
            "cpc": cpc,
            "competition_index": competition,
        }
        for keyword, vol, monthly, cpc, competition in (related or [])
    ]
    with (
        patch(
            "app.services.indicators.pytrends.pytrends_stored_volume",
            AsyncMock(return_value=volume),
        ),
        patch(
            "app.services.indicators.pytrends.pytrends_stored_related",
            AsyncMock(return_value=related_payload),
        ),
    ):
        yield
