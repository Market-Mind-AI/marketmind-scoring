"""HTTP client for dispatching on-demand collection to the 4 sibling
services and polling their ingestion runs to completion.

Connectivity: each service is reached over its host-exposed HTTP port via
``host.docker.internal`` — same rationale as the read paths (the 4
services' docker networks are not actually shared despite the common name).
See README "Conectividade entre serviços" and memory marketmind_core_architecture.

Contract quirks handled here (confirmed by reading each service's actual
schemas/routes, not assumed):
- All 4 collection POST endpoints return ``{"metadata": {"ingestion_run_id": ...}}``.
- ``GET /runs/{id}`` returns the run flat (``{"status": ...}``) for PyTrends,
  WorldBank and OECD, but UNData wraps it as ``{"data": {"run": {"status": ...}}}``.
- UNData's raw status vocabulary is wider (pending/running/success/partial/failed)
  than the other 3 (running/success/failed) — normalized via IngestionRunStatus.
"""

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from app import observability
from app.core.config import settings
from app.core.exceptions import MarketMindCoreError
from app.db.models.country import Country
from app.domain.enums import DataSource, IngestionRunStatus
from app.services import demand_spend

_BASE_URLS: dict[DataSource, str] = {
    DataSource.PYTRENDS: settings.pytrends_api_base_url,
    DataSource.WORLDBANK: settings.worldbank_api_base_url,
    DataSource.OECD: settings.oecd_api_base_url,
    DataSource.UNDATA: settings.undata_api_base_url,
}

_RAW_STATUS_MAP: dict[str, IngestionRunStatus] = {
    "pending": IngestionRunStatus.RUNNING,
    "running": IngestionRunStatus.RUNNING,
    "success": IngestionRunStatus.SUCCESS,
    # "sem imputação": a partially-succeeded run does not count as coverage.
    "partial": IngestionRunStatus.FAILED,
    "failed": IngestionRunStatus.FAILED,
}

# The collector service is now Google Ads keyword data (via DataForSEO), not
# Google Trends (2026-07-15). search_volume backs the demand LEVEL + growth +
# stability (its monthly_searches array); keywords_for_keywords backs intent.
_PYTRENDS_ENDPOINTS: dict[str, str] = {
    "search_volume": "/search-volume",
    "keywords_for_keywords": "/keywords-for-keywords",
}


class CollectionDispatchError(MarketMindCoreError):
    """A gap couldn't be dispatched: missing required collection_params,
    missing country code mapping in the canonical `countries` table, or the
    external service rejected/failed the request."""


class CollectionTimeoutError(MarketMindCoreError):
    """Polling exceeded settings.collection_poll_timeout_seconds without the
    run reaching a terminal state. The run itself keeps going server-side —
    this only bounds how long the Core waits synchronously."""


@dataclass
class CollectionRequest:
    source: DataSource
    indicator_code: str
    # WorldBank, OECD and UNData need it to resolve the source's own country
    # code. The demand collector (pytrends) does not read it: its location and
    # language travel in collection_params — see _build_pytrends_request.
    country: Country | None
    collection_params: dict[str, Any]


def _require(params: dict[str, Any], key: str) -> Any:
    value = params.get(key)
    if value in (None, ""):
        raise CollectionDispatchError(f"collection_params.{key} is required for this gap")
    return value


def resolve_source_country_code(source: DataSource, country: Country) -> str:
    if source == DataSource.PYTRENDS:
        code = country.pytrends_geo or country.iso2
    elif source == DataSource.WORLDBANK:
        code = country.worldbank_country_id or country.iso3
    elif source == DataSource.OECD:
        code = country.oecd_ref_area or country.iso3
    else:  # UNDATA
        code = country.iso2
    if not code:
        raise CollectionDispatchError(f"No {source.value} country code mapping for {country.iso3}")
    return code


def _require_country_code(source: DataSource, country: Country | None) -> str:
    if country is None:
        raise CollectionDispatchError(f"{source.value} requires a country, none was given")
    return resolve_source_country_code(source, country)


def _build_pytrends_request(indicator_code: str, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    path = _PYTRENDS_ENDPOINTS.get(indicator_code)
    if path is None:
        raise CollectionDispatchError(f"Unknown pytrends indicator_code '{indicator_code}'")

    # location is the ISO2 country code Google Ads expects; language is the
    # DataForSEO language_code. Both, plus the term list, are set by
    # analysis_service from the product's localized_markets[ISO3].
    payload: dict[str, Any] = {
        "location": _require(params, "location"),
        "language": _require(params, "language"),
    }
    if indicator_code == "search_volume":
        payload["keywords"] = _require(params, "keywords")
    else:  # keywords_for_keywords
        payload["seed_keywords"] = _require(params, "seed_keywords")
        if params.get("limit") is not None:
            payload["limit"] = params["limit"]
    return path, payload


def _build_request(req: CollectionRequest) -> tuple[str, dict[str, Any]]:
    params = req.collection_params

    if req.source == DataSource.PYTRENDS:
        return _build_pytrends_request(req.indicator_code, params)

    if req.source == DataSource.WORLDBANK:
        return "/indicators/collect", {
            "country": _require_country_code(DataSource.WORLDBANK, req.country),
            "start_year": _require(params, "start_year"),
            "end_year": _require(params, "end_year"),
            "indicator_codes": [req.indicator_code],
        }

    if req.source == DataSource.OECD:
        payload: dict[str, Any] = {
            "country": _require_country_code(DataSource.OECD, req.country),
            "indicator_codes": [req.indicator_code],
        }
        if "start_period" in params:
            payload["start_period"] = params["start_period"]
        if "end_period" in params:
            payload["end_period"] = params["end_period"]
        return "/indicators/collect", payload

    # UNDATA — needs the UN M49 numeric location_id, no ISO-code fallback exists.
    undata_country_code = _require_country_code(DataSource.UNDATA, req.country)
    location_id = req.country.undata_location_id if req.country is not None else None
    if not location_id:
        country_label = req.country.iso3 if req.country is not None else "<none>"
        raise CollectionDispatchError(f"No UN Data location_id mapping for {country_label}")
    return "/indicators/collect", {
        "country": undata_country_code,
        "location_id": int(location_id),
        "start_year": _require(params, "start_year"),
        "end_year": _require(params, "end_year"),
        "indicator_codes": [req.indicator_code],
    }


def _extract_ingestion_run_id(source: DataSource, body: dict[str, Any]) -> uuid.UUID:
    # UNData's /indicators/collect never fills metadata.ingestion_run_id (it
    # can create more than one run per call) — the real id is a list at
    # data.ingestion_run_ids. Since collection_client always dispatches
    # exactly one indicator_code per gap, that list always has exactly one
    # entry in practice. Confirmed by reading the service's own code
    # (app/services/undata_service.py::collect_indicators), not assumed.
    if source == DataSource.UNDATA:
        run_ids = body.get("data", {}).get("ingestion_run_ids") or []
        if not run_ids:
            raise CollectionDispatchError(f"{source.value} collection response missing data.ingestion_run_ids")
        return uuid.UUID(str(run_ids[0]))

    raw = body.get("metadata", {}).get("ingestion_run_id")
    if raw is None:
        raise CollectionDispatchError(f"{source.value} collection response missing metadata.ingestion_run_id")
    return uuid.UUID(str(raw))


def _extract_run_status(source: DataSource, body: dict[str, Any]) -> IngestionRunStatus:
    raw_status = body["data"]["run"]["status"] if source == DataSource.UNDATA else body["status"]
    normalized = _RAW_STATUS_MAP.get(raw_status)
    if normalized is None:
        raise CollectionDispatchError(f"Unknown {source.value} ingestion run status '{raw_status}'")
    return normalized


def _why(exc: BaseException) -> str:
    """O que falhou, em palavras, mesmo quando a exceção não traz nenhuma.

    httpx levanta seus timeouts com mensagem VAZIA, e um objeto de exceção é
    sempre truthy — então `{exc or type(exc).__name__}` mantém a mensagem
    vazia e o relato termina nos dois pontos. Foi assim que um timeout de dez
    minutos da API do World Bank no CPI da Colômbia chegou em branco a quem
    chamou. Sem mensagem, o nome da classe é o fato.
    """
    return str(exc) or type(exc).__name__


def _charge(observation: observability.Observation, headers: httpx.Headers) -> None:
    """What the collector was billed for this dispatch: into the request's
    tally, and onto the dispatch's observation as its cost."""
    reported = demand_spend.read(headers)
    if reported is None:
        return
    demand_spend.add(reported)
    detail: dict[str, Any] = {"dataforseo_tasks": reported.calls}
    if reported.calls_without_cost:
        detail["dataforseo_tasks_without_cost"] = reported.calls_without_cost
    observation.update(metadata=detail)
    # The bill itself, priced as DataForSEO states it (cost_details is taken
    # as given, no model table involved), as a child the SDK will carry a
    # cost on. Named after the vendor, not the source: it is the invoice line.
    observation.record(
        "dataforseo",
        model="dataforseo/google-ads",
        usage_details={"tasks": reported.calls},
        cost_details={"total": reported.usd},
        metadata={"provider": "DataForSEO"},
    )
    if reported.calls_without_cost:
        observation.update(
            level="WARNING",
            status_message=f"DataForSEO reported no cost for {reported.calls_without_cost} of {reported.calls} tasks",
        )


class CollectionClient:
    """One instance per call site; a fresh httpx.AsyncClient per request
    keeps this stateless. Call volume here is bounded by job_task creation
    (gap-driven), not a hot path, so pooling is not worth the complexity."""

    async def dispatch(self, req: CollectionRequest) -> uuid.UUID:
        """Ask the source's service to collect one gap. Returns its ingestion run id.

        One observation in the analysis's trace per dispatch, named after the
        source, with its latency and — for the Google Ads collector — what
        DataForSEO billed for it, read from the response headers the collector
        sets (its app/core/spend.py) and relayed on this request's own tally
        (demand_spend). The only cost of an analysis that is not a model, put
        in the trace next to the call that spent it.
        """
        path, payload = _build_request(req)
        base_url = _BASE_URLS[req.source]
        country = req.country.iso3 if req.country is not None else None
        with observability.observe(
            f"collect {req.source.value}",
            input={"indicator_code": req.indicator_code, "country": country, "request": payload},
            metadata={"source": req.source.value, "indicator_code": req.indicator_code, "country": country},
        ) as observation:
            try:
                async with httpx.AsyncClient(
                    base_url=base_url, timeout=settings.collection_dispatch_timeout_seconds
                ) as client:
                    response = await client.post(path, json=payload)
                    # Before the status check: a task DataForSEO failed is
                    # still billed, and the collector says so on its error
                    # responses too.
                    _charge(observation, response.headers)
                    response.raise_for_status()
                    run_id = _extract_ingestion_run_id(req.source, response.json())
            except httpx.HTTPError as exc:
                # Some httpx exceptions (e.g. ReadTimeout) stringify to an empty
                # message — always include the exception type so a failure is
                # still diagnosable from job_tasks.error_message alone.
                raise CollectionDispatchError(
                    f"Failed to dispatch {req.source.value} collection: {type(exc).__name__}: {exc}"
                ) from exc
            observation.update(output={"ingestion_run_id": str(run_id)})
            return run_id

    async def freshness(
        self, source: DataSource, *, countries: list[str], indicators: list[str], stale_after_days: int
    ) -> dict[tuple[str, str], bool]:
        """Ask a collection service when it last LOOKED, per country and
        indicator. Returns {(country, indicator_code): is_stale}.

        The service that owns the data is the one that knows this. Keeping a
        second copy of the answer here — which is what coverage_index was —
        meant two records of the same fact, and when they disagreed the stale
        one won: a collection the pytrends service had just said it needed was
        skipped because the local index still called it fresh.

        One request per source for every pair at once, not one per gap. An
        unreachable service raises, and the caller decides — treating "I could
        not ask" as "it is fresh" would silently skip collection.
        """
        base_url = _BASE_URLS[source]
        params = [("country", c) for c in countries] + [("indicator", i) for i in indicators]
        params.append(("stale_after_days", str(stale_after_days)))
        try:
            async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
                response = await client.get("/freshness", params=params)
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPError as exc:
            raise CollectionDispatchError(f"{source.value} freshness failed: {_why(exc)}") from exc

        items = (body.get("data") or {}).get("items") or []
        return {(item["country"], item["indicator_code"]): bool(item.get("stale", True)) for item in items}

    async def poll_until_done(self, source: DataSource, ingestion_run_id: uuid.UUID) -> IngestionRunStatus:
        base_url = _BASE_URLS[source]
        deadline = time.monotonic() + settings.collection_poll_timeout_seconds
        try:
            async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
                while True:
                    response = await client.get(f"/runs/{ingestion_run_id}")
                    response.raise_for_status()
                    status = _extract_run_status(source, response.json())
                    if status != IngestionRunStatus.RUNNING:
                        return status
                    if time.monotonic() >= deadline:
                        raise CollectionTimeoutError(
                            f"{source.value} ingestion run {ingestion_run_id} still running after "
                            f"{settings.collection_poll_timeout_seconds}s"
                        )
                    await asyncio.sleep(settings.collection_poll_interval_seconds)
        except httpx.HTTPError as exc:
            raise CollectionDispatchError(f"Failed to poll {source.value} run {ingestion_run_id}: {_why(exc)}") from exc
