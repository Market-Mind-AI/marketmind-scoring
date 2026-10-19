"""Model calls are priced here, by the table Langfuse holds, so a run can say
what it cost before Langfuse has ingested its trace (2026-10-19)."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import observability, pricing
from app.services.llm import usage
from app.services.llm.traced import TracedProvider
from tests.test_llm_tracing import FakeProvider


def _model(name: str, pattern: str, prices: dict[str, float], *, managed: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        model_name=name,
        match_pattern=pattern,
        prices={k: SimpleNamespace(price=v) for k, v in prices.items()},
        is_langfuse_managed=managed,
    )


OPUS = _model(
    "claude-opus-5",
    r"(?i)^((anthropic\/)?claude-opus-5|(eu\.|us\.)?anthropic\.claude-opus-5(-v1(:0)?)?)$",
    {
        "input": 5e-6,
        "output": 25e-6,
        "input_cache_read": 5e-7,
        "input_cache_creation_5m": 6.25e-6,
        "input_cache_creation_1h": 1e-5,
    },
)
GPT = _model("gpt-5.1", r"(?i)^(openai/)?(gpt-5.1)$", {"input": 1.25e-6, "output": 1e-5})


class _Models:
    """Langfuse's models API, two pages deep."""

    def __init__(self, pages: list[list[SimpleNamespace]], fail: bool = False) -> None:
        self.pages, self.fail, self.asked = pages, fail, []

    def list(self, *, limit: int, page: int) -> SimpleNamespace:
        self.asked.append(page)
        if self.fail:
            raise RuntimeError("langfuse down")
        return SimpleNamespace(data=self.pages[page - 1], meta=SimpleNamespace(total_pages=len(self.pages)))


@pytest.fixture(autouse=True)
def _empty_table():
    pricing._table.clear()
    yield
    pricing._table.clear()


def _client(models: _Models) -> SimpleNamespace:
    return SimpleNamespace(api=SimpleNamespace(models=models))


def test_the_table_is_read_whole_across_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    models = _Models([[OPUS], [GPT]])
    monkeypatch.setattr(observability, "client", lambda: _client(models))

    assert pricing.load() == 2
    assert models.asked == [1, 2]
    assert pricing.loaded()


def test_a_call_is_priced_by_usage_type_the_way_langfuse_prices_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(observability, "client", lambda: _client(_Models([[OPUS, GPT]])))
    pricing.load()

    # 200 uncached in, 900 cache read, 100 written for an hour, 300 out
    usd = pricing.price(
        "claude-opus-5", {"input": 200, "output": 300, "input_cache_read": 900, "input_cache_creation_1h": 100}
    )

    assert usd == pytest.approx(200 * 5e-6 + 300 * 25e-6 + 900 * 5e-7 + 100 * 1e-5)
    assert pricing.price("anthropic/claude-opus-5", {"input": 1}) == pytest.approx(5e-6), "the regex, not the name"
    assert pricing.price("gpt-5.1", {"input": 1000, "output": 100}) == pytest.approx(1.25e-3 + 1e-3)


def test_unknown_is_none_never_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(observability, "client", lambda: _client(_Models([[OPUS]])))
    pricing.load()

    assert pricing.price("gemma4:12b", {"input": 10, "output": 1}) is None, "a model the table does not know"
    assert pricing.price("claude-opus-5", None) is None, "a provider that reported no usage"
    assert pricing.price("claude-opus-5", {"input": 10, "output_reasoning": 5}) is None, "a usage type without a price"
    assert pricing.price("claude-opus-5", {"input": 10, "total": 10}) == pytest.approx(5e-5), "total is not a type"


def test_without_a_table_nothing_is_priced() -> None:
    assert not pricing.loaded()
    assert pricing.price("claude-opus-5", {"input": 10}) is None


def test_the_projects_own_definition_wins_over_the_managed_one(monkeypatch: pytest.MonkeyPatch) -> None:
    own = _model("claude-opus-5", r"(?i)^claude-opus-5$", {"input": 1.0}, managed=False)
    monkeypatch.setattr(observability, "client", lambda: _client(_Models([[OPUS, own]])))
    pricing.load()

    assert pricing.price("claude-opus-5", {"input": 1}) == 1.0


def test_a_failed_read_keeps_the_previous_table_and_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(observability, "client", lambda: _client(_Models([[OPUS]])))
    pricing.load()
    monkeypatch.setattr(observability, "client", lambda: _client(_Models([], fail=True)))

    assert pricing.load() == 1
    assert pricing.price("claude-opus-5", {"input": 1}) == pytest.approx(5e-6)


def test_without_langfuse_there_is_no_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(observability, "client", lambda: None)

    assert pricing.load() == 0


# --- the tally and the headers -----------------------------------------------


@pytest.fixture
def priced(monkeypatch: pytest.MonkeyPatch):
    """A table loaded and a Langfuse client that exports nowhere."""
    import uuid

    from langfuse import Langfuse
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    client = Langfuse(
        public_key=f"pk-test-{uuid.uuid4()}",
        secret_key="sk-test",
        host="http://127.0.0.1:9",
        span_exporter=InMemorySpanExporter(),
    )
    monkeypatch.setattr(observability, "client", lambda: client)
    pricing._table[:] = pricing._definitions([OPUS, GPT])


async def test_each_call_is_charged_to_the_requests_tally(priced) -> None:
    opus = TracedProvider(FakeProvider(tokens=(1000, 100), model="claude-opus-5"), site="enrichment")
    gpt = TracedProvider(FakeProvider(tokens=(1000, 100), model="gpt-5.1"), site="catalog_match")

    with usage.collect() as spent:
        await opus.complete(user="x")
        await gpt.complete(user="x")

    assert spent.cost_usd == pytest.approx(1000 * 5e-6 + 100 * 25e-6 + 1000 * 1.25e-6 + 100 * 1e-5)
    assert spent.cost_by_model == {
        "claude-opus-5": pytest.approx(1000 * 5e-6 + 100 * 25e-6),
        "gpt-5.1": pytest.approx(1000 * 1.25e-6 + 100 * 1e-5),
    }
    assert spent.calls_without_cost == 0


async def test_a_call_that_cannot_be_priced_is_counted_not_zeroed(priced) -> None:
    unknown = TracedProvider(FakeProvider(tokens=(1000, 100), model="gemma4:12b"), site="enrichment")
    silent = TracedProvider(FakeProvider(no_usage=True, model="claude-opus-5"), site="enrichment")

    with usage.collect() as spent:
        await unknown.complete(user="x")
        await silent.complete(user="x")

    assert spent.cost_usd == 0.0
    assert spent.calls_without_cost == 2


async def test_a_failed_call_is_still_charged(priced) -> None:
    provider = TracedProvider(
        FakeProvider(error=RuntimeError("boom"), tokens=(1000, 0), model="claude-opus-5"), site="x"
    )

    with usage.collect() as spent, pytest.raises(RuntimeError):
        await provider.complete(user="x")

    assert spent.cost_usd == pytest.approx(1000 * 5e-6)


def test_the_response_carries_the_dollars_next_to_the_tokens() -> None:
    app = FastAPI()

    from app.main import report_llm_usage

    app.middleware("http")(report_llm_usage)

    @app.get("/thinks")
    def thinks() -> dict[str, str]:
        usage.record("claude-opus-5", SimpleNamespace(input_tokens=1000, output_tokens=100))
        usage.charge("claude-opus-5", 0.0075)
        usage.record("gpt-5.1", SimpleNamespace(input_tokens=10, output_tokens=1))
        usage.charge("gpt-5.1", 0.0001)
        usage.record("gemma4:12b", SimpleNamespace(input_tokens=10, output_tokens=1))
        usage.charge("gemma4:12b", None)
        return {"ok": "yes"}

    response = TestClient(app).get("/thinks")

    assert response.headers["X-LLM-Cost-USD"] == "0.007600"
    assert response.headers[
        "X-LLM-Cost-By-Model"
    ] == "claude-opus-5=0.007500,gemma4:12b=0.000000,gpt-5.1=0.000100".replace("gemma4:12b=0.000000,", "")
    assert response.headers["X-LLM-Calls-Without-Cost"] == "1"
