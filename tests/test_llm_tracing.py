"""Each model call becomes one Langfuse generation, named after its call site."""

import json
import uuid
from types import SimpleNamespace

import pytest
from langfuse import Langfuse
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel

from app import observability
from app.services.llm import usage
from app.services.llm.base import ImageInput, LLMProvider
from app.services.llm.traced import TracedProvider, describe_image


class FakeProvider(LLMProvider):
    """Stands in for Claude/GPT/Ollama: the only thing that is faked, since the
    real ones spend money."""

    def __init__(self, reply: object = "ok", error: Exception | None = None, tokens: tuple[int, int] = (0, 0)):
        super().__init__("fake-model")
        self.reply, self.error, self.tokens = reply, error, tokens
        self.calls: list[dict] = []

    async def complete(self, *, system=None, user, image=None, response_model=None):
        self.calls.append({"system": system, "user": user, "image": image, "response_model": response_model})
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
        return {s.name: {"attrs": dict(s.attributes), "trace_id": format(s.context.trace_id, "032x")}
                for s in exporter.get_finished_spans()}

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
    assert json.loads(_attr(gen, "output")) == "café colombiano"
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


async def test_a_failed_call_is_marked_and_the_error_is_unchanged(spans) -> None:
    boom = RuntimeError("provider down")
    provider = TracedProvider(FakeProvider(error=boom), site="reference_market")

    with pytest.raises(RuntimeError) as raised:
        await provider.complete(user="x")

    assert raised.value is boom
    gen = spans()["reference_market"]
    assert _attr(gen, "level") == "ERROR"
    assert _attr(gen, "status_message") == "provider down"


async def test_a_tracing_failure_does_not_fail_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken:
        def start_observation(self, **_: object) -> None:
            raise RuntimeError("langfuse exploded")

    monkeypatch.setattr(observability, "client", lambda: Broken())

    assert await TracedProvider(FakeProvider(reply="fine"), site="enrichment").complete(user="x") == "fine"


def test_the_placeholder_reads_in_megabytes_for_a_large_photo() -> None:
    assert describe_image(ImageInput(base64_data="A" * 3_066_668, media_type="image/jpeg")) == "[image/jpeg, 2.3 MB]"


def test_current_is_the_open_tally_or_none() -> None:
    assert usage.current() is None
    with usage.collect() as spend:
        assert usage.current() is spend
    assert usage.current() is None
