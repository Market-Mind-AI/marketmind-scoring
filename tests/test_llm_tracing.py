"""Each model call becomes one Langfuse generation, named after its call site."""

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langfuse import Langfuse
from langfuse.api import Prompt_Text
from langfuse.model import TextPromptClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel

from app import observability, prompts
from app.core.config import settings
from app.services.llm import resolver, usage
from app.services.llm.base import ImageInput, LLMProvider
from app.services.llm.providers import OllamaProvider
from app.services.llm.traced import TracedProvider, describe_image


class FakeProvider(LLMProvider):
    """Stands in for Claude/GPT/Ollama: the only thing that is faked, since the
    real ones spend money."""

    def __init__(
        self,
        reply: object = "ok",
        error: Exception | None = None,
        tokens: tuple[int, int] = (0, 0),
        no_usage: bool = False,
    ):
        super().__init__("fake-model")
        self.reply, self.error, self.tokens, self.no_usage = reply, error, tokens, no_usage
        self.calls: list[dict] = []

    async def complete(self, *, system=None, user, image=None, response_model=None):
        self.calls.append({"system": system, "user": user, "image": image, "response_model": response_model})
        if self.no_usage:
            usage.record(self.model, None)
        else:
            usage.record(self.model, SimpleNamespace(input_tokens=self.tokens[0], output_tokens=self.tokens[1]))
        if self.error:
            raise self.error
        return self.reply


class Verdict(BaseModel):
    same_product: bool
    reason: str


@pytest.fixture
def spans(monkeypatch: pytest.MonkeyPatch):
    """A real Langfuse client exporting to memory instead of the network."""
    exporter = InMemorySpanExporter()
    client = Langfuse(
        public_key=f"pk-test-{uuid.uuid4()}", secret_key="sk-test", host="http://127.0.0.1:9", span_exporter=exporter
    )
    monkeypatch.setattr(observability, "client", lambda: client)

    def finished() -> dict[str, dict]:
        client.flush()
        return {
            s.name: {"attrs": dict(s.attributes), "trace_id": format(s.context.trace_id, "032x")}
            for s in exporter.get_finished_spans()
        }

    return finished


def _attr(span: dict, key: str) -> object:
    return span["attrs"].get(f"langfuse.observation.{key}")


async def test_a_call_becomes_a_generation_named_after_its_site(spans) -> None:
    provider = TracedProvider(FakeProvider(reply="café colombiano"), site="enrichment")

    result = await provider.complete(system="You localise.", user="Café colombiano 500 g")

    assert result == "café colombiano"
    gen = spans()["enrichment"]
    assert _attr(gen, "type") == "generation"
    assert _attr(gen, "model.name") == "fake-model"
    assert json.loads(_attr(gen, "input")) == {"system": "You localise.", "user": "Café colombiano 500 g"}
    assert _attr(gen, "output") == "café colombiano"
    assert _attr(gen, "metadata.provider") == "FakeProvider"


async def test_a_structured_answer_is_recorded_as_its_fields(spans) -> None:
    provider = TracedProvider(FakeProvider(reply=Verdict(same_product=True, reason="alias")), site="catalog_match")

    await provider.complete(user="x", response_model=Verdict)

    assert json.loads(_attr(spans()["catalog_match"], "output")) == {"same_product": True, "reason": "alias"}


async def test_the_photo_never_reaches_langfuse(spans) -> None:
    photo = ImageInput(base64_data="A" * 4000, media_type="image/png")
    provider = TracedProvider(FakeProvider(), site="vision")

    await provider.complete(user="What is this?", image=photo)

    recorded = _attr(spans()["vision"], "input")
    assert "AAAA" not in recorded
    assert json.loads(recorded)["image"] == "[image/png, 3 KB]"


async def test_the_inner_provider_still_gets_the_real_photo(spans) -> None:
    photo = ImageInput(base64_data="A" * 4000, media_type="image/png")
    inner = FakeProvider()

    await TracedProvider(inner, site="vision").complete(user="What is this?", image=photo)

    assert inner.calls[0]["image"] is photo


async def test_tokens_come_from_the_requests_tally(spans) -> None:
    provider = TracedProvider(FakeProvider(tokens=(1200, 300)), site="explanation")

    with usage.collect():
        usage.record("earlier-call", SimpleNamespace(input_tokens=50, output_tokens=5))
        await provider.complete(user="Explain")

    assert json.loads(_attr(spans()["explanation"], "usage_details")) == {"input": 1200, "output": 300}


class CachedProvider(FakeProvider):
    async def complete(self, *, system=None, system_tail=None, user, image=None, response_model=None):
        self.calls.append({"system": system, "system_tail": system_tail, "user": user})
        usage.record(
            self.model,
            SimpleNamespace(
                input_tokens=200, output_tokens=300, cache_read_input_tokens=900, cache_creation_input_tokens=100
            ),
        )
        return self.reply


async def test_cached_tokens_are_priced_apart_from_the_rest_of_the_prompt(spans) -> None:
    """Langfuse prices each usage type on its own: the whole prompt under
    `input` would bill cache reads at the full rate."""
    inner = CachedProvider()
    provider = TracedProvider(inner, site="explanation")

    with usage.collect():
        await provider.complete(system="Stable.", system_tail="Brand: Cardhu.", user="Explain")

    assert inner.calls[0]["system_tail"] == "Brand: Cardhu."
    assert json.loads(_attr(spans()["explanation"], "usage_details")) == {
        "input": 200,
        "output": 300,
        "input_cache_read": 900,
        "input_cache_creation": 100,
    }


async def test_a_failed_call_is_marked_and_the_error_is_unchanged(spans) -> None:
    boom = RuntimeError("provider down")
    provider = TracedProvider(FakeProvider(error=boom), site="reference_market")

    with pytest.raises(RuntimeError) as raised:
        await provider.complete(user="x")

    assert raised.value is boom
    gen = spans()["reference_market"]
    assert _attr(gen, "level") == "ERROR"
    # The type too, as the orchestrator's spans say it ("ScoringUnavailable:
    # ..."), so a status reads the same wherever in the trace it appears.
    assert _attr(gen, "status_message") == "RuntimeError: provider down"


async def test_a_failed_calls_tokens_still_reach_its_generation(spans) -> None:
    """The most expensive failures — an unparseable structured answer — still
    spent the tokens the provider reports before it raises. Losing them from
    the generation made the priciest errors look free."""
    provider = TracedProvider(FakeProvider(error=RuntimeError("unparseable"), tokens=(900, 16000)), site="enrichment")

    with usage.collect():
        with pytest.raises(RuntimeError):
            await provider.complete(user="x")

    gen = spans()["enrichment"]
    assert json.loads(_attr(gen, "usage_details")) == {"input": 900, "output": 16000}
    assert _attr(gen, "level") == "ERROR"


async def test_a_cancelled_call_still_ends_its_generation(spans) -> None:
    """A bare `except Exception` around the provider call would let a
    CancelledError skip _finish entirely, leaving the generation open forever."""
    provider = TracedProvider(FakeProvider(error=asyncio.CancelledError()), site="enrichment")

    with pytest.raises(asyncio.CancelledError):
        await provider.complete(user="x")

    gen = spans()["enrichment"]
    assert _attr(gen, "level") == "ERROR"


async def test_a_call_with_no_reported_usage_is_not_recorded_as_free(spans) -> None:
    provider = TracedProvider(FakeProvider(no_usage=True), site="enrichment")

    with usage.collect():
        await provider.complete(user="x")

    assert _attr(spans()["enrichment"], "usage_details") is None


async def test_a_tracing_failure_does_not_fail_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken:
        def start_observation(self, **_: object) -> None:
            raise RuntimeError("langfuse exploded")

    monkeypatch.setattr(observability, "client", lambda: Broken())

    assert await TracedProvider(FakeProvider(reply="fine"), site="enrichment").complete(user="x") == "fine"


async def test_a_failed_image_description_does_not_fail_the_call(spans) -> None:
    with patch("app.services.llm.traced.describe_image", side_effect=RuntimeError("describe failed")):
        result = await TracedProvider(FakeProvider(reply="fine"), site="vision").complete(
            user="x", image=ImageInput(base64_data="AA", media_type="image/png")
        )

    assert result == "fine"


def test_the_placeholder_reads_in_megabytes_for_a_large_photo() -> None:
    assert describe_image(ImageInput(base64_data="A" * 3_066_668, media_type="image/jpeg")) == "[image/jpeg, 2.3 MB]"


def test_current_is_the_open_tally_or_none() -> None:
    assert usage.current() is None
    with usage.collect() as spend:
        assert usage.current() is spend
    assert usage.current() is None


def test_the_resolver_names_each_site(spans) -> None:
    sites = {
        resolver.resolve_vision_llm: "vision",
        resolver.resolve_enrichment_llm: "enrichment",
        resolver.resolve_reference_market_llm: "reference_market",
        resolver.resolve_explanation_llm: "explanation",
        resolver.resolve_catalog_match_llm: "catalog_match",
    }
    with patch.object(settings, "anthropic_api_key", "test-key"), patch.object(settings, "openai_api_key", "test-key"):
        for resolve, site in sites.items():
            provider = resolve()
            assert isinstance(provider, TracedProvider), resolve.__name__
            assert provider.site == site


def test_without_tracing_the_resolver_returns_the_bare_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(observability, "client", lambda: None)

    provider = resolver.resolve_llm(provider="ollama", model="gemma4:12b", site="enrichment")

    assert isinstance(provider, OllamaProvider)


def test_a_request_carrying_traceparent_puts_its_calls_in_that_trace(spans) -> None:
    from app.main import adopt_request_trace

    app = FastAPI()
    app.middleware("http")(adopt_request_trace)

    @app.post("/probe")
    async def probe() -> dict:
        await TracedProvider(FakeProvider(), site="enrichment").complete(user="x")
        return {}

    trace_id = "99eadfa97c641404b5c53a5958a11f14"
    TestClient(app).post("/probe", headers={"traceparent": f"00-{trace_id}-b88f74cfd738d647-01"})

    assert spans()["enrichment"]["trace_id"] == trace_id


async def test_a_generation_is_linked_to_the_prompt_version_it_used(spans, monkeypatch: pytest.MonkeyPatch) -> None:
    stored = TextPromptClient(
        Prompt_Text(
            name="enrichment-system",
            version=4,
            prompt="You localise.",
            labels=["production"],
            tags=[],
            config={},
        )
    )
    monkeypatch.setattr(observability.client(), "get_prompt", lambda name, **_: stored)
    # prompt_text only fetches from Langfuse for a name the known set already
    # confirms (F1, app/prompts.py) — warm it here so the fetch above runs.
    prompts.warm({"enrichment-system": "the code text"})

    system = prompts.prompt_text("enrichment-system", "the code text")
    await TracedProvider(FakeProvider(), site="enrichment").complete(system=system, user="x")

    gen = spans()["enrichment"]
    assert _attr(gen, "prompt.name") == "enrichment-system"
    assert _attr(gen, "prompt.version") == 4
    assert json.loads(_attr(gen, "input"))["system"] == "You localise."


async def test_a_call_without_a_fetched_prompt_is_not_linked(spans) -> None:
    await TracedProvider(FakeProvider(), site="explanation").complete(user="x")

    assert _attr(spans()["explanation"], "prompt.name") is None


# --- the levels every generation reports (2026-10-19) ------------------------
#
# The Tracing page's Quality view filters on level ERROR or WARNING. Only
# exceptions were ERROR, and nothing was ever WARNING, so a degraded call — one
# that could not be priced, or one that ran on the code text instead of the
# prompt Langfuse holds — looked exactly like a healthy one.


async def test_a_call_with_no_reported_usage_is_a_warning_that_says_so(spans) -> None:
    provider = TracedProvider(FakeProvider(no_usage=True), site="enrichment")

    with usage.collect():
        await provider.complete(user="x")

    gen = spans()["enrichment"]
    assert _attr(gen, "level") == "WARNING"
    assert _attr(gen, "status_message") == "the provider reported no token usage; the call is not priced"


async def test_a_prompt_from_the_code_text_is_a_warning_naming_the_prompt(spans) -> None:
    """Langfuse is configured but has not confirmed the name, so the model ran
    on the code text and the generation cannot be linked to a prompt version."""
    prompts._known.discard("enrichment-system")
    system = prompts.prompt_text("enrichment-system", "the code text")

    await TracedProvider(FakeProvider(), site="enrichment").complete(system=system, user="x")

    gen = spans()["enrichment"]
    assert _attr(gen, "level") == "WARNING"
    assert _attr(gen, "status_message") == (
        "prompt enrichment-system came from the code text (not confirmed by Langfuse yet)"
    )
    assert _attr(gen, "prompt.name") is None


async def test_a_linked_prompt_leaves_the_level_alone(spans, monkeypatch: pytest.MonkeyPatch) -> None:
    stored = TextPromptClient(
        Prompt_Text(
            name="enrichment-system", version=4, prompt="You localise.", labels=["production"], tags=[], config={}
        )
    )
    monkeypatch.setattr(observability.client(), "get_prompt", lambda name, **_: stored)
    prompts.warm({"enrichment-system": "the code text"})
    system = prompts.prompt_text("enrichment-system", "the code text")

    await TracedProvider(FakeProvider(tokens=(10, 5)), site="enrichment").complete(system=system, user="x")

    gen = spans()["enrichment"]
    assert _attr(gen, "level") in (None, "DEFAULT"), "unset is DEFAULT in Langfuse"
    assert _attr(gen, "status_message") is None


async def test_two_warnings_are_both_said(spans) -> None:
    prompts._known.discard("enrichment-system")
    system = prompts.prompt_text("enrichment-system", "the code text")

    with usage.collect():
        await TracedProvider(FakeProvider(no_usage=True), site="enrichment").complete(system=system, user="x")

    message = _attr(spans()["enrichment"], "status_message")
    assert "prompt enrichment-system came from the code text" in message
    assert "reported no token usage" in message


async def test_an_error_outranks_a_warning_and_names_the_exception_type(spans) -> None:
    prompts._known.discard("enrichment-system")
    system = prompts.prompt_text("enrichment-system", "the code text")

    with pytest.raises(RuntimeError):
        await TracedProvider(FakeProvider(error=RuntimeError("provider down")), site="enrichment").complete(
            system=system, user="x"
        )

    gen = spans()["enrichment"]
    assert _attr(gen, "level") == "ERROR"
    assert _attr(gen, "status_message") == "RuntimeError: provider down"


async def test_without_langfuse_a_fallback_is_not_a_warning_waiting_to_happen(monkeypatch) -> None:
    """Unconfigured means untraced: the reason must not sit in the context
    variable and surface on some later call once tracing is configured."""
    monkeypatch.setattr(observability, "client", lambda: None)

    prompts.prompt_text("enrichment-system", "the code text")

    assert prompts.take_fallback_reason() is None


class TtlCachedProvider(FakeProvider):
    """Anthropic's usage, with the cache write split by TTL the way the API
    reports it (usage.cache_creation.ephemeral_*_input_tokens)."""

    async def complete(self, *, system=None, system_tail=None, user, image=None, response_model=None):
        usage.record(
            self.model,
            SimpleNamespace(
                input_tokens=200,
                output_tokens=300,
                cache_read_input_tokens=900,
                cache_creation_input_tokens=100,
                cache_creation=SimpleNamespace(ephemeral_5m_input_tokens=0, ephemeral_1h_input_tokens=100),
            ),
        )
        return self.reply


async def test_a_cache_write_is_priced_by_its_ttl_when_the_provider_says_which(spans) -> None:
    """A 1-hour write bills at 2x and a 5-minute one at 1.25x; under the
    undifferentiated key Langfuse prices every write at the 5-minute rate."""
    with usage.collect():
        await TracedProvider(TtlCachedProvider(), site="explanation").complete(system="Stable.", user="Explain")

    assert json.loads(_attr(spans()["explanation"], "usage_details")) == {
        "input": 200,
        "output": 300,
        "input_cache_read": 900,
        "input_cache_creation_1h": 100,
    }


def test_the_tally_splits_cache_writes_by_ttl() -> None:
    with usage.collect() as spend:
        usage.record(
            "m",
            SimpleNamespace(
                input_tokens=10,
                output_tokens=1,
                cache_creation_input_tokens=30,
                cache_creation=SimpleNamespace(ephemeral_5m_input_tokens=10, ephemeral_1h_input_tokens=20),
            ),
        )

    assert (spend.cache_write_tokens, spend.cache_write_5m_tokens, spend.cache_write_1h_tokens) == (30, 10, 20)
