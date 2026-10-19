"""What DataForSEO billed for a run's demand data reaches the trace and the
orchestrator, instead of stopping at the collector's log (2026-10-19)."""

import json
import uuid

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langfuse import Langfuse
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import observability
from app.domain.enums import DataSource
from app.services import demand_spend
from app.services.collection_client import CollectionClient, CollectionDispatchError, CollectionRequest
from tests.test_collection_client import _country

# --- the tally ---------------------------------------------------------------


def test_a_collector_response_is_read_from_its_headers() -> None:
    reported = demand_spend.read(
        httpx.Headers(
            {"X-DataForSEO-Cost-USD": "0.057500", "X-DataForSEO-Calls": "3", "X-DataForSEO-Calls-Without-Cost": "1"}
        )
    )

    assert reported == demand_spend.Spend(usd=0.0575, calls=3, calls_without_cost=1)


def test_a_response_from_a_source_that_costs_nothing_reports_nothing() -> None:
    assert demand_spend.read(httpx.Headers({"content-type": "application/json"})) is None


def test_the_tally_adds_up_every_dispatch_of_the_request() -> None:
    with demand_spend.collect() as spent:
        demand_spend.add(demand_spend.Spend(usd=0.05, calls=1))
        demand_spend.add(None)
        demand_spend.add(demand_spend.Spend(usd=0.0075, calls=2, calls_without_cost=1))

    assert (spent.usd, spent.calls, spent.calls_without_cost) == (0.0575, 3, 1)
    assert demand_spend.headers(spent) == {
        "X-DataForSEO-Cost-USD": "0.057500",
        "X-DataForSEO-Calls": "3",
        "X-DataForSEO-Calls-Without-Cost": "1",
    }
    assert demand_spend.headers(demand_spend.Spend()) == {}


def test_outside_a_request_nothing_is_counted_and_nothing_breaks() -> None:
    demand_spend.add(demand_spend.Spend(usd=0.05, calls=1))

    assert demand_spend.current() is None


# --- the dispatch -----------------------------------------------------------


@pytest.fixture
def spans(monkeypatch: pytest.MonkeyPatch):
    exporter = InMemorySpanExporter()
    client = Langfuse(
        public_key=f"pk-test-{uuid.uuid4()}", secret_key="sk-test", host="http://127.0.0.1:9", span_exporter=exporter
    )
    monkeypatch.setattr(observability, "client", lambda: client)

    def finished() -> dict[str, dict]:
        client.flush()
        return {
            s.name: {
                **dict(s.attributes),
                "span_id": s.context.span_id,
                "parent": s.parent.span_id if s.parent else None,
            }
            for s in exporter.get_finished_spans()
        }

    return finished


def _attr(span: dict, key: str) -> object:
    return span.get(f"langfuse.observation.{key}")


def _collector_answers(monkeypatch: pytest.MonkeyPatch, *, status: int, headers: dict[str, str], body: dict) -> None:
    async def _post(self, path: str, json: dict) -> httpx.Response:  # noqa: ANN001
        return httpx.Response(status, headers=headers, json=body, request=httpx.Request("POST", path))

    monkeypatch.setattr(httpx.AsyncClient, "post", _post)


def _pytrends_request() -> CollectionRequest:
    return CollectionRequest(
        source=DataSource.PYTRENDS,
        indicator_code="search_volume",
        country=_country(),
        collection_params={"location": "ES", "language": "es", "keywords": ["freidora de aire"]},
    )


async def test_a_dispatch_puts_what_dataforseo_billed_on_its_observation_and_the_tally(
    monkeypatch: pytest.MonkeyPatch, spans
) -> None:
    run_id = uuid.uuid4()
    _collector_answers(
        monkeypatch,
        status=200,
        headers={"X-DataForSEO-Cost-USD": "0.050000", "X-DataForSEO-Calls": "1"},
        body={"metadata": {"ingestion_run_id": str(run_id)}},
    )

    with demand_spend.collect() as spent:
        assert await CollectionClient().dispatch(_pytrends_request()) == run_id

    assert (spent.usd, spent.calls) == (0.05, 1)
    exported = spans()
    span = exported["collect pytrends"]
    assert _attr(span, "type") == "tool"
    assert json.loads(_attr(span, "output")) == {"ingestion_run_id": str(run_id)}
    assert _attr(span, "metadata.country") == "ESP"
    assert _attr(span, "metadata.dataforseo_tasks") == 1
    assert _attr(span, "level") in (None, "DEFAULT")
    # The bill is a child the SDK carries a cost on, under the dispatch.
    bill = exported["dataforseo"]
    assert bill["parent"] == span["span_id"]
    assert json.loads(_attr(bill, "cost_details")) == {"total": 0.05}
    assert json.loads(_attr(bill, "usage_details")) == {"tasks": 1}
    assert _attr(bill, "model.name") == "dataforseo/google-ads"


async def test_a_failed_collection_is_still_charged_and_marked_as_an_error(
    monkeypatch: pytest.MonkeyPatch, spans
) -> None:
    """DataForSEO bills a task it fails, and the collector says so on its 502."""
    _collector_answers(
        monkeypatch,
        status=502,
        headers={"X-DataForSEO-Cost-USD": "0.050000", "X-DataForSEO-Calls": "1"},
        body={"detail": "DataForSEO task failed (40501): Invalid Field: 'language_code'"},
    )

    with demand_spend.collect() as spent, pytest.raises(CollectionDispatchError):
        await CollectionClient().dispatch(_pytrends_request())

    assert spent.usd == 0.05
    exported = spans()
    span = exported["collect pytrends"]
    assert _attr(span, "level") == "ERROR"
    assert "HTTPStatusError" in str(_attr(span, "status_message"))
    assert json.loads(_attr(exported["dataforseo"], "cost_details")) == {"total": 0.05}


async def test_a_source_that_costs_nothing_is_traced_without_a_cost(monkeypatch: pytest.MonkeyPatch, spans) -> None:
    run_id = uuid.uuid4()
    _collector_answers(monkeypatch, status=200, headers={}, body={"metadata": {"ingestion_run_id": str(run_id)}})
    request = CollectionRequest(
        source=DataSource.WORLDBANK,
        indicator_code="NY.GDP.PCAP.CD",
        country=_country(),
        collection_params={"start_year": 2020, "end_year": 2025},
    )

    with demand_spend.collect() as spent:
        await CollectionClient().dispatch(request)

    assert spent.calls == 0
    exported = spans()
    assert "dataforseo" not in exported, "no bill, no invoice line"
    assert _attr(exported["collect worldbank"], "metadata.indicator_code") == "NY.GDP.PCAP.CD"


async def test_unpriced_tasks_are_a_warning_on_the_dispatch(monkeypatch: pytest.MonkeyPatch, spans) -> None:
    _collector_answers(
        monkeypatch,
        status=200,
        headers={
            "X-DataForSEO-Cost-USD": "0.000000",
            "X-DataForSEO-Calls": "1",
            "X-DataForSEO-Calls-Without-Cost": "1",
        },
        body={"metadata": {"ingestion_run_id": str(uuid.uuid4())}},
    )

    await CollectionClient().dispatch(_pytrends_request())

    span = spans()["collect pytrends"]
    assert _attr(span, "level") == "WARNING"
    assert _attr(span, "status_message") == "DataForSEO reported no cost for 1 of 1 tasks"


async def test_without_tracing_a_dispatch_still_works_and_still_tallies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(observability, "client", lambda: None)
    run_id = uuid.uuid4()
    _collector_answers(
        monkeypatch,
        status=200,
        headers={"X-DataForSEO-Cost-USD": "0.050000", "X-DataForSEO-Calls": "1"},
        body={"metadata": {"ingestion_run_id": str(run_id)}},
    )

    with demand_spend.collect() as spent:
        assert await CollectionClient().dispatch(_pytrends_request()) == run_id

    assert spent.usd == 0.05


# --- the relay to the orchestrator -------------------------------------------


def test_the_response_relays_the_dataforseo_spend_next_to_the_tokens() -> None:
    app = FastAPI()

    from app.main import report_llm_usage

    app.middleware("http")(report_llm_usage)

    @app.get("/collects")
    def collects() -> dict[str, str]:
        demand_spend.add(demand_spend.Spend(usd=0.05, calls=1))
        demand_spend.add(demand_spend.Spend(usd=0.0075, calls=2, calls_without_cost=1))
        return {"ok": "yes"}

    @app.get("/does-not-collect")
    def does_not_collect() -> dict[str, str]:
        return {"ok": "yes"}

    client = TestClient(app)

    spent = client.get("/collects")
    assert spent.headers["X-DataForSEO-Cost-USD"] == "0.057500"
    assert spent.headers["X-DataForSEO-Calls"] == "3"
    assert spent.headers["X-DataForSEO-Calls-Without-Cost"] == "1"
    assert "X-LLM-Calls" not in spent.headers

    free = client.get("/does-not-collect")
    assert "X-DataForSEO-Calls" not in free.headers
