"""Reads the collected data from the four collection services over HTTP.

This replaces four read-only SQL connections into databases the Core does not
own. Reading another service's tables works right up until that service renames
a column, and then a calculation here breaks with nothing in either test suite
to catch it. The services now serve what they hold, and the shape of what they
serve is a contract rather than an implementation detail.

Every function here raises on a transport failure rather than answering with an
empty series. The difference is the whole point: "the source has no value for
this country" and "we could not ask" lead to opposite decisions -- the first is
declared and the country is delivered without it, the second is retried. An
empty list returned on a timeout would quietly turn every outage into a
permanent gap in the data.
"""

from typing import Any

import httpx

from app.core.config import settings
from app.core.exceptions import ExternalSourceUnavailableError
from app.domain.enums import DataSource

# The collection services sit next to this one; a read that takes longer than
# this is a service in trouble, not a slow query.
_TIMEOUT = httpx.Timeout(30.0, connect=5.0)

_BASE_URLS: dict[DataSource, str] = {
    DataSource.PYTRENDS: settings.pytrends_api_base_url,
    DataSource.WORLDBANK: settings.worldbank_api_base_url,
    DataSource.OECD: settings.oecd_api_base_url,
    DataSource.UNDATA: settings.undata_api_base_url,
}


async def _get(source: DataSource, path: str, params: dict[str, Any]) -> dict[str, Any]:
    url = f"{_BASE_URLS[source].rstrip('/')}{path}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as exc:
        raise ExternalSourceUnavailableError(f"{source.value} read failed at {path}: {exc}") from exc


async def _post(source: DataSource, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{_BASE_URLS[source].rstrip('/')}{path}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as exc:
        raise ExternalSourceUnavailableError(f"{source.value} read failed at {path}: {exc}") from exc


async def worldbank_series(country_iso2: str, indicator_code: str, limit: int | None = None) -> list[tuple[int, float]]:
    """(year, value) newest first, nulls already dropped by the service."""
    params: dict[str, Any] = {"country_iso2": country_iso2, "indicator_code": indicator_code}
    if limit is not None:
        params["limit"] = limit
    body = await _get(DataSource.WORLDBANK, "/observations/series", params)
    return [(int(p["year"]), float(p["value"])) for p in body["data"]["points"]]


async def oecd_series(
    country: str,
    measure: str | None = None,
    frequency: str | None = None,
    limit: int | None = None,
) -> list[tuple[str, float]]:
    """(time_period, value) newest first, one value per period.

    The deduplication is the service's now. It used to be done here, because
    re-collection stores a period many times over and reading the raw table
    returned the same quarter repeatedly -- which once made a twelve-quarter
    window collapse into one and produced a growth rate of exactly zero for
    every country.
    """
    params: dict[str, Any] = {"country": country}
    if measure is not None:
        params["measure"] = measure
    if frequency is not None:
        params["frequency"] = frequency
    if limit is not None:
        params["limit"] = limit
    body = await _get(DataSource.OECD, "/observations/series", params)
    return [(str(p["time_period"]), float(p["value"])) for p in body["data"]["points"]]


async def undata_series(country: str, indicator_code: str, limit: int | None = None) -> list[tuple[str, float]]:
    """(time_period, value) newest first.

    Called without a limit when two indicators have to be lined up year by year:
    the years they cover need not match, and the overlap is only knowable after
    reading both in full.
    """
    params: dict[str, Any] = {"country": country, "indicator_code": indicator_code}
    if limit is not None:
        params["limit"] = limit
    body = await _get(DataSource.UNDATA, "/observations/series", params)
    return [(str(p["time_period"]), float(p["value"])) for p in body["data"]["points"]]


async def pytrends_stored_volume(location: str, keywords: list[str]) -> dict[str, Any]:
    """What is stored for these keywords, plus `missing` -- the ones that are
    not, or whose stored value is past `demand_stale_after_days`.

    Nothing is collected and nothing is billed. `missing` comes from the
    service rather than being worked out here: the match has to be
    case-insensitive, because that source lowercases what it stores, and a
    curated term written with capitals would look missing forever and be
    re-collected on every run at real cost.

    A idade entra no pedido porque a política é daqui, não de lá: o serviço
    responde o que tem e quando coletou; quem decide o que é velho demais é
    quem vai usar o número.
    """
    if not keywords:
        return {"keywords": [], "missing": []}
    body = await _post(
        DataSource.PYTRENDS,
        "/stored/search-volume",
        {
            "location": location,
            "keywords": keywords,
            "stale_after_days": settings.demand_stale_after_days,
        },
    )
    return body["data"]


async def pytrends_stored_related(location: str, seed_keyword: str) -> list[dict[str, Any]]:
    """The related keywords stored for one seed, freshest row per keyword."""
    body = await _get(DataSource.PYTRENDS, "/stored/related", {"location": location, "seed_keyword": seed_keyword})
    return body["data"]["keywords"]
