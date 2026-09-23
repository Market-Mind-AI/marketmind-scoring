# Langfuse tracing for core's LLM calls: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every LLM call made by `marketmind-core` shows up as a Langfuse generation. When the orchestrator's analysis run causes the call, the generation lands inside that run's trace.

**Architecture:**
- **Core:** a `TracedProvider` wrapper opens one Langfuse generation per `complete()` call. The resolver applies it around whichever provider it built. A middleware adopts the W3C `traceparent` the caller sends.
- **Orchestrator:** `ScoringClient._headers()` injects the current OpenTelemetry context, which is the run's `analysis` span.

**Tech stack:** Python 3.12+, FastAPI, Langfuse Python SDK v4 (OpenTelemetry based), `opentelemetry.propagate`, pytest with `asyncio_mode = "auto"`, and Poetry.

**Spec:** `docs/superpowers/specs/2026-09-21-langfuse-llm-tracing-design.md`

## Global constraints

- `langfuse = "^4.15.0"` (same as the orchestrator).
- Tracing turns on only when `LANGFUSE_PUBLIC_KEY` **and** `LANGFUSE_SECRET_KEY` are set. `LANGFUSE_HOST` defaults to `https://cloud.langfuse.com`.
- Tracing never raises into a model call. A tracing failure is logged at debug level, and the call's result or exception is returned unchanged.
- The product photo never reaches Langfuse. It is replaced by the placeholder `[<media_type>, <size>]`.
- Generation names are the site names: `vision`, `enrichment`, `reference_market`, `catalog_match`, `explanation`.
- **No paid API calls without the user's explicit approval** (Anthropic, DataForSEO, OpenAI). Every test uses a fake provider. Only Task 6 spends money, and only after the user says yes.
- Delivery is one branch and PR per repo: core `feat/langfuse-llm-tracing` (already exists, and carries the spec) and orchestrator `feat/langfuse-llm-tracing` (created from `fix/langfuse-scores`). Never commit to `main` or `master`.
- Commit messages follow each repo's style: a plain imperative sentence, no `feat:` prefix, ending with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- Never print `.env` values. Read keys into shell variables only.

## File map

| File | Repo | Responsibility |
|---|---|---|
| `app/core/config.py` | core | 3 new settings: `langfuse_public_key`, `langfuse_secret_key`, `langfuse_host` |
| `app/observability.py` (new) | core | `client()`, `tracing_enabled()`, `adopt_trace_context(headers)` |
| `app/services/llm/usage.py` | core | new public `current()` accessor |
| `app/services/llm/traced.py` (new) | core | `TracedProvider(inner, site)` and the image placeholder |
| `app/services/llm/resolver.py` | core | `resolve_llm(..., site=None)` wraps when tracing is on; each `resolve_*_llm` passes its site |
| `app/main.py` | core | `adopt_request_trace` middleware and a startup warning when untraced |
| `pyproject.toml`, `.env.example` | core | dependency and documented keys |
| `tests/test_observability.py`, `tests/test_llm_tracing.py` (new) | core | tests |
| `app/scoring_client.py` | orchestrator | `_headers()` injects `traceparent` |
| `tests/test_scoring_client.py` | orchestrator | tests |

## How to run core's tests

Core's `tests/conftest.py` refuses any database whose name lacks "test", and truncates tables after each test. Task 1 creates `marketmind_test` on core's Docker Postgres (host port 5436). From then on:

```bash
cd /Users/miguel/Projects/market-mind-org/marketmind-core
export DATABASE_HOST=localhost DATABASE_PORT=5436 DATABASE_NAME=marketmind_test
poetry run pytest tests/<file>.py -v
```

---

### Task 1: Core settings, `observability` module and test DB

**Files:**
- Modify: `pyproject.toml` (`[tool.poetry.dependencies]`), `.env.example` (after the `ANTHROPIC_API_KEY=` block), `app/core/config.py` (after `anthropic_workspace_id: str = ""`, around line 200)
- Create: `app/observability.py`, `tests/test_observability.py`

**Interfaces:**
- Produces:
  - `observability.client() -> Langfuse | None` (cached)
  - `observability.tracing_enabled() -> bool`
  - `observability.adopt_trace_context(headers: Mapping[str, str])`, a context manager
  - Tests replace the client by monkeypatching the **module attribute** `observability.client`, so callers must call `observability.client()` through the module, never `from app.observability import client`.

- [ ] **Step 1: Set up the core dev environment and test DB**

```bash
cd /Users/miguel/Projects/market-mind-org/marketmind-core
git branch --show-current            # expect: feat/langfuse-llm-tracing
poetry env use python3.14 && poetry install --no-interaction
docker exec marketmind-scoring-postgres createdb -U marketmind marketmind_test
export DATABASE_HOST=localhost DATABASE_PORT=5436 DATABASE_NAME=marketmind_test
poetry run alembic upgrade head
poetry run pytest -q -p no:warnings 2>&1 | tail -3   # baseline: note pass/fail counts
git status --porcelain               # if poetry.lock appears untracked, do NOT commit it (core has no lock)
```

- [ ] **Step 2: Add the dependency and the settings**

In `pyproject.toml` under `[tool.poetry.dependencies]`, add:

```toml
langfuse = "^4.15.0"
```

Run `poetry install --no-interaction` again.

In `app/core/config.py`, directly after `anthropic_workspace_id: str = ""`:

```python
    # Langfuse tracing of the model calls (app/observability.py). Both keys or
    # nothing: unset means untraced, which is allowed — a missing trace loses an
    # explanation, it does not corrupt an answer.
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"
```

In `.env.example`, after the `ANTHROPIC_API_KEY=` line, add:

```bash

# --- Tracing ---
# Every model call becomes a generation in Langfuse: the prompt, the answer, the
# tokens and the latency. Joined to the orchestrator's trace for the analysis
# that caused it. Unset means untraced, which is allowed.
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=https://cloud.langfuse.com
```

- [ ] **Step 3: Write the failing tests**

Create `tests/test_observability.py`:

```python
"""Tracing is optional, and when on it joins the caller's trace."""

from unittest.mock import patch

import pytest
from langfuse import Langfuse

from app import observability
from app.core.config import settings


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    observability.client.cache_clear()
    yield
    observability.client.cache_clear()


def test_without_keys_there_is_no_tracing() -> None:
    with patch.object(settings, "langfuse_public_key", ""), patch.object(settings, "langfuse_secret_key", ""):
        assert observability.client() is None
        assert observability.tracing_enabled() is False


def test_one_key_alone_is_not_enough() -> None:
    with patch.object(settings, "langfuse_public_key", "pk-test"), patch.object(settings, "langfuse_secret_key", ""):
        assert observability.tracing_enabled() is False


def test_with_both_keys_there_is_a_client() -> None:
    with (
        patch.object(settings, "langfuse_public_key", "pk-test-core"),
        patch.object(settings, "langfuse_secret_key", "sk-test"),
        patch.object(settings, "langfuse_host", "http://127.0.0.1:9"),
    ):
        assert isinstance(observability.client(), Langfuse)
        assert observability.tracing_enabled() is True


def test_adopting_a_callers_traceparent_makes_it_the_current_trace() -> None:
    from opentelemetry import trace

    trace_id = "99eadfa97c641404b5c53a5958a11f14"
    headers = {"traceparent": f"00-{trace_id}-b88f74cfd738d647-01"}

    with observability.adopt_trace_context(headers):
        inside = trace.get_current_span().get_span_context()
    outside = trace.get_current_span().get_span_context()

    assert format(inside.trace_id, "032x") == trace_id
    assert not outside.is_valid, "the caller's context must not leak past the request"
```

- [ ] **Step 4: Run the tests to confirm they fail**

Run: `poetry run pytest tests/test_observability.py -v`
Expected: FAIL (`ImportError: cannot import name 'observability' from 'app'`).

- [ ] **Step 5: Implement `app/observability.py`**

```python
"""Tracing for the model calls this service makes.

The orchestrator decides; the model calls that decision causes happen here. So
its trace can say which step ran but not what a model was asked, what it
answered, or what that cost — unless this service reports them into the same
trace. The orchestrator sends the W3C `traceparent` of its run with every
request; adopting it makes each model call a child of the step that caused it.

Unconfigured means untraced, and that is allowed: a missing trace loses an
explanation, it does not corrupt an answer.
"""

import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from functools import lru_cache
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry.propagate import extract

from app.core.config import settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def client() -> Any | None:
    """The Langfuse client, or None when tracing is not configured."""
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        return None

    from langfuse import Langfuse

    return Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        host=settings.langfuse_host,
    )


def tracing_enabled() -> bool:
    return client() is not None


@contextmanager
def adopt_trace_context(headers: Mapping[str, str]) -> Iterator[None]:
    """Make the caller's trace the current one for the length of a request.

    Without a `traceparent` this is a no-op: calls start traces of their own,
    which is right for the browser's identify-image, made before any analysis
    exists.
    """
    token = otel_context.attach(extract(headers))
    try:
        yield
    finally:
        otel_context.detach(token)
```

- [ ] **Step 6: Run the tests to confirm they pass**

Run: `poetry run pytest tests/test_observability.py -v`
Expected: 4 passed.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml .env.example app/core/config.py app/observability.py tests/test_observability.py
git commit -m "Add optional Langfuse tracing and adopt the caller's trace

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: `usage.current()` and `TracedProvider`

**Files:**
- Modify: `app/services/llm/usage.py` (after `collect()`)
- Create: `app/services/llm/traced.py`, `tests/test_llm_tracing.py`

**Interfaces:**
- Consumes: `observability.client()` (Task 1). `LLMProvider`, `ImageInput` from `app.services.llm.base`. `usage.collect()`, `usage.record()`.
- Produces:
  - `usage.current() -> Usage | None`
  - `TracedProvider(inner: LLMProvider, site: str)`, an `LLMProvider` whose `.model == inner.model`, with attributes `.inner` and `.site`
  - `describe_image(image: ImageInput) -> str`, returning e.g. `"[image/png, 3 KB]"`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_llm_tracing.py`:

```python
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
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `poetry run pytest tests/test_llm_tracing.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'app.services.llm.traced'`).

- [ ] **Step 3: Add `usage.current()`**

In `app/services/llm/usage.py`, directly after the `collect()` function:

```python
def current() -> Usage | None:
    """The tally open for this request, or None outside one."""
    return _current.get()
```

- [ ] **Step 4: Implement `app/services/llm/traced.py`**

```python
"""One Langfuse generation per model call, whatever the provider.

A wrapper rather than a change in each provider: the providers are pure
transport (see base.py), and tracing belongs to the call site — which prompt,
for which step — not to who answered. The site name becomes the generation's
name, so a trace reads "enrichment", "explanation", not "ClaudeProvider".

Tracing never fails a call. Every Langfuse operation is guarded; the provider's
answer, or its exception, passes through unchanged.
"""

import logging
from typing import Any

from pydantic import BaseModel

from app import observability
from app.services.llm import usage
from app.services.llm.base import ImageInput, LLMProvider

logger = logging.getLogger(__name__)


def describe_image(image: ImageInput) -> str:
    """What the trace shows instead of the photo. The photo stays here."""
    size = len(image.base64_data) * 3 // 4
    readable = f"{size / 1_000_000:.1f} MB" if size >= 1_000_000 else f"{round(size / 1000)} KB"
    return f"[{image.media_type}, {readable}]"


class TracedProvider(LLMProvider):
    def __init__(self, inner: LLMProvider, site: str) -> None:
        super().__init__(inner.model)
        self.inner = inner
        self.site = site

    async def complete(
        self,
        *,
        system: str | None = None,
        user: str,
        image: ImageInput | None = None,
        response_model: type[BaseModel] | None = None,
    ) -> str | BaseModel:
        generation = self._start(system, user, image)
        before = _tokens(usage.current())
        try:
            result = await self.inner.complete(system=system, user=user, image=image, response_model=response_model)
        except Exception as exc:
            _finish(generation, level="ERROR", status_message=str(exc))
            raise
        _finish(generation, output=_as_output(result), usage_details=_spent_since(before))
        return result

    def _start(self, system: str | None, user: str, image: ImageInput | None) -> Any | None:
        client = observability.client()
        if client is None:
            return None
        recorded: dict[str, Any] = {"system": system, "user": user} if system else {"user": user}
        if image is not None:
            recorded["image"] = describe_image(image)
        try:
            return client.start_observation(
                name=self.site,
                as_type="generation",
                model=self.model,
                input=recorded,
                metadata={"provider": type(self.inner).__name__},
            )
        except Exception:  # noqa: BLE001 — tracing never fails a call
            logger.debug("Could not open the %s generation", self.site, exc_info=True)
            return None


def _as_output(result: str | BaseModel) -> Any:
    return result.model_dump(mode="json") if isinstance(result, BaseModel) else result


def _tokens(tally: usage.Usage | None) -> tuple[int, int] | None:
    return (tally.input_tokens, tally.output_tokens) if tally is not None else None


def _spent_since(before: tuple[int, int] | None) -> dict[str, int] | None:
    after = _tokens(usage.current())
    if before is None or after is None:
        return None
    return {"input": after[0] - before[0], "output": after[1] - before[1]}


def _finish(generation: Any | None, **update: Any) -> None:
    if generation is None:
        return
    try:
        generation.update(**{k: v for k, v in update.items() if v is not None})
        generation.end()
    except Exception:  # noqa: BLE001 — tracing never fails a call
        logger.debug("Could not close a generation", exc_info=True)
```

Note: the system prompt is recorded only when present, so the first test's expected input includes `system` because it passes one.

- [ ] **Step 5: Run the tests to confirm they pass**

Run: `poetry run pytest tests/test_llm_tracing.py -v`
Expected: 9 passed.

- [ ] **Step 6: Commit**

```bash
git add app/services/llm/usage.py app/services/llm/traced.py tests/test_llm_tracing.py
git commit -m "Record each model call as a Langfuse generation named after its site

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Wire the resolver and the request middleware

**Files:**
- Modify: `app/services/llm/resolver.py` (all of it, 51 lines), `app/main.py` (lifespan around line 27, and a new middleware next to `report_llm_usage` around line 68)
- Test: `tests/test_llm_tracing.py` (append)

**Interfaces:**
- Consumes: `TracedProvider`, `observability.tracing_enabled()`, `observability.adopt_trace_context()`
- Produces:
  - `resolve_llm(*, provider: str, model: str, site: str | None = None) -> LLMProvider`. With `site` and tracing on, it returns a `TracedProvider`. Otherwise it returns the bare provider, which keeps existing callers and `tests/test_llm_resolver.py` unchanged.
  - `app.main.adopt_request_trace`, a middleware function

- [ ] **Step 1: Write the failing tests** (append to `tests/test_llm_tracing.py`)

```python
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import settings
from app.services.llm import resolver
from app.services.llm.providers import OllamaProvider


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
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `poetry run pytest tests/test_llm_tracing.py -v -k "resolver or traceparent"`
Expected: FAIL. `test_the_resolver_names_each_site` fails on `isinstance(..., TracedProvider)`, and the middleware test fails with `ImportError: cannot import name 'adopt_request_trace'`.

- [ ] **Step 3: Wire the resolver**

Replace `app/services/llm/resolver.py` lines 6–51 with:

```python
from app import observability
from app.core.config import settings
from app.services.llm.base import LLMError, LLMProvider
from app.services.llm.providers import ClaudeProvider, GptProvider, OllamaProvider
from app.services.llm.traced import TracedProvider


def resolve_llm(*, provider: str, model: str, site: str | None = None) -> LLMProvider:
    """``site`` names the call site in the trace; without it, or without
    tracing configured, the bare provider comes back."""
    resolved = _build(provider, model)
    if site and observability.tracing_enabled():
        return TracedProvider(resolved, site)
    return resolved


def _build(provider: str, model: str) -> LLMProvider:
    code = provider.strip().lower()
    if code == "claude":
        return ClaudeProvider(model, settings.anthropic_api_key, settings.anthropic_workspace_id)
    if code == "gpt":
        return GptProvider(model, settings.openai_api_key)
    if code == "ollama":
        return OllamaProvider(model, settings.ollama_llm_api_base_url, settings.ollama_llm_timeout_seconds)
    raise LLMError(f"Unknown LLM provider '{provider}' (expected one of: claude, gpt, ollama)")


def resolve_vision_llm() -> LLMProvider:
    """The identify-image call (a vision model)."""
    return resolve_llm(provider=settings.vision_llm_provider, model=settings.vision_llm_model, site="vision")


def resolve_enrichment_llm() -> LLMProvider:
    """Fills the JSON fields vision left out — above all localized_markets
    (a TEXT model, since the local vision model is unreliable at producing
    localized search terms)."""
    return resolve_llm(
        provider=settings.enrichment_llm_provider, model=settings.enrichment_llm_model, site="enrichment"
    )


def resolve_reference_market_llm() -> LLMProvider:
    """Chooses the reference-market candidates (the product's biggest markets) —
    a real-market-knowledge task, so it defaults to a cloud LLM."""
    return resolve_llm(
        provider=settings.reference_market_llm_provider,
        model=settings.reference_market_llm_model,
        site="reference_market",
    )


def resolve_explanation_llm() -> LLMProvider:
    """The final MOS-narrative call (text)."""
    return resolve_llm(
        provider=settings.explanation_llm_provider, model=settings.explanation_llm_model, site="explanation"
    )


def resolve_catalog_match_llm() -> LLMProvider:
    """Decide se o texto consultado é o mesmo produto de alguma entidade do
    catálogo, quando alias exato e trigram falham (ver catalog_matcher.py).
    Substituiu o passo de embedding: os casos que sobram são sinônimo de
    domínio e idiomas sem parentesco de escrita, que distância vetorial não
    resolve."""
    return resolve_llm(
        provider=settings.catalog_match_llm_provider, model=settings.catalog_match_llm_model, site="catalog_match"
    )
```

- [ ] **Step 4: Add the middleware and the startup warning in `app/main.py`**

Add the import next to the other `app.` imports:

```python
from app import observability
```

In `lifespan`, directly after the first `logger.info(...)` line:

```python
    if not observability.tracing_enabled():
        logger.warning("Langfuse is not configured; model calls will not be traced.")
```

Directly after the `report_llm_usage` middleware function, add:

```python
@app.middleware("http")
async def adopt_request_trace(request: Request, call_next) -> Response:
    """Make the caller's trace the current one for this request.

    The orchestrator sends its run's W3C `traceparent`; adopting it puts every
    model call this request makes inside that run's trace, under the step that
    asked. A request without one — the browser's identify-image — starts its own.
    """
    with observability.adopt_trace_context(request.headers):
        return await call_next(request)
```

- [ ] **Step 5: Run the tracing tests and the full suite**

Run: `poetry run pytest tests/test_llm_tracing.py tests/test_llm_resolver.py tests/test_observability.py -v`
Expected: all pass (12 in `test_llm_tracing.py`).

Run: `poetry run pytest -q -p no:warnings 2>&1 | tail -3`
Expected: the same counts as the Task 1 baseline, plus the new tests.

Run: `poetry run ruff check app tests` if `ruff` is configured. Expected: no new findings.

- [ ] **Step 6: Commit**

```bash
git add app/services/llm/resolver.py app/main.py tests/test_llm_tracing.py
git commit -m "Trace every call site and join the caller's trace per request

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Orchestrator sends the run's `traceparent`

**Files:**
- Modify: `/Users/miguel/Projects/market-mind-org/marketmind-orchestrator/app/scoring_client.py` (`_headers()`, around line 60, plus the imports)
- Test: `/Users/miguel/Projects/market-mind-org/marketmind-orchestrator/tests/test_scoring_client.py` (append)

**Interfaces:**
- Consumes: `observability.run(analysis_id)` (on `fix/langfuse-scores`) and `observability._handler`
- Produces: every `ScoringClient` request made during a run carries `traceparent`, whose trace id is `Langfuse.create_trace_id(seed=analysis_id)`

- [ ] **Step 1: Create the branch**

```bash
cd /Users/miguel/Projects/market-mind-org/marketmind-orchestrator
git checkout fix/langfuse-scores && git checkout -b feat/langfuse-llm-tracing
```

- [ ] **Step 2: Write the failing tests** (append to `tests/test_scoring_client.py`)

```python
def test_a_call_made_during_a_run_carries_the_runs_trace(monkeypatch) -> None:
    """So the model calls it causes in the scoring service land inside the run's
    trace, under the step that asked, instead of in traces of their own."""
    from langfuse import Langfuse

    from app import observability
    from app.scoring_client import ScoringClient

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv("LANGFUSE_HOST", "http://localhost:3000")
    observability._handler.cache_clear()
    analysis_id = "a8c5d38b-db60-413c-b8f3-086a0befb83a"

    try:
        with observability.run(analysis_id):
            headers = ScoringClient(base_url="http://core", token="t").headers()
    finally:
        observability._handler.cache_clear()

    assert headers["X-Internal-Token"] == "t"
    assert headers["traceparent"].split("-")[1] == Langfuse.create_trace_id(seed=analysis_id)


def test_a_call_outside_a_run_carries_no_trace() -> None:
    from app.scoring_client import ScoringClient

    assert "traceparent" not in ScoringClient(base_url="http://core", token="t").headers()
```

- [ ] **Step 3: Run the tests to confirm they fail**

Run: `env -u LANGFUSE_PUBLIC_KEY -u LANGFUSE_SECRET_KEY poetry run pytest tests/test_scoring_client.py -v -k trace`
Expected: `test_a_call_made_during_a_run_carries_the_runs_trace` FAILS with `KeyError: 'traceparent'`, and the other passes.

- [ ] **Step 4: Implement**

In `app/scoring_client.py`, add to the imports:

```python
from opentelemetry.propagate import inject
```

Replace `_headers()` with:

```python
    def _headers(self) -> dict[str, str]:
        # An absent token is not sent as an empty one: the service answers 401
        # either way, and a header that is present but empty reads like a
        # configuration that was attempted rather than one that was never made.
        headers = {"X-Internal-Token": self._token} if self._token else {}
        # The run's trace, so the model calls this request causes in the scoring
        # service land inside it. Outside a run nothing is current and nothing
        # is added.
        inject(headers)
        return headers
```

- [ ] **Step 5: Run the tests to confirm they pass, then run the full suite**

Run: `env -u LANGFUSE_PUBLIC_KEY -u LANGFUSE_SECRET_KEY poetry run pytest -q -p no:warnings 2>/dev/null | tail -2`
Expected: 113 passed (111 before, plus 2).

Run: `poetry run ruff check app tests`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add app/scoring_client.py tests/test_scoring_client.py
git commit -m "Send the run's trace with every call to the scoring service

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Deploy locally and verify for free

**Files:** `marketmind-core/.env` (local and gitignored, never committed)

- [ ] **Step 1: Copy the Langfuse keys into core's `.env` without printing them**

```bash
cd /Users/miguel/Projects/market-mind-org
O=marketmind-orchestrator/.env C=marketmind-core/.env
for k in LANGFUSE_PUBLIC_KEY LANGFUSE_SECRET_KEY LANGFUSE_HOST; do
  V="$(grep -m1 "^$k=" "$O" | cut -d= -f2-)"
  grep -q "^$k=" "$C" || printf '%s=%s\n' "$k" "$V" >> "$C"
done
for k in LANGFUSE_PUBLIC_KEY LANGFUSE_SECRET_KEY LANGFUSE_HOST; do grep -q "^$k=.\+" "$C" && echo "$k set"; done
```

Expected: three `set` lines.

- [ ] **Step 2: Rebuild and restart both services**

```bash
(cd marketmind-core && docker compose up -d --build marketmind-scoring-api)
(cd marketmind-orchestrator && docker compose up -d --build marketmind-orchestrator-api)
until curl -sf localhost:8004/health >/dev/null && curl -sf localhost:8081/healthz >/dev/null; do sleep 2; done
docker logs marketmind-scoring-api 2>&1 | grep -c "Langfuse is not configured"   # expect 0
```

- [ ] **Step 3: Free end-to-end check that the header crosses the wire**

A single `GET /countries` through the orchestrator makes no model call. So this check only proves both services are up with the new code. The tracing path itself is proven by Task 3's middleware test.

```bash
curl -s -o /dev/null -w '%{http_code}\n' localhost:8081/countries   # expect 200
```

---

### Task 6: Live check. **Paid, so ask the user first.**

- [ ] **Step 1: Ask the user for approval, stating the cost**

Tell the user:

> "The live check makes real Claude calls: one `identify-image` (the `vision` site, text-only hint, no photo) for a **new** product, then one `/analyze` for a **new** country (the `enrichment` site, and `reference_market` if it runs). That's about 3 calls and roughly 10k tokens, well under US$0.50. DataForSEO stays in sandbox (free). A new product and country are needed because the Mexico coffee vocabulary is cached and makes no model call, and because of the OECD timezone bug on repeat countries. OK to run?"

Proceed only on an explicit yes.

- [ ] **Step 2: Run it**

```bash
curl -s -X POST localhost:8081/catalog/identify-image -H 'Content-Type: application/json' \
  -d '{"image_base64":null,"image_media_type":null,"hint_text":"Aceite de oliva virgen extra, botella de 1 litro","countries":["CHL"]}' \
  -o /tmp/mm_identify2.json -w 'HTTP %{http_code}\n'
python3 - <<'EOF'
import json, urllib.request
d = json.load(open('/tmp/mm_identify2.json'))
body = {k: d.get(k) for k in ['product_name','brand','category','localized_markets','reference_candidates','target_age_min','target_age_max','estimated_price','price_currency']}
body['countries'] = ['CHL']
r = urllib.request.urlopen(urllib.request.Request('http://localhost:8081/analyze', data=json.dumps(body).encode(), headers={'Content-Type':'application/json'}, method='POST'))
out = json.load(r); print(out); open('/tmp/mm_request_id3','w').write(out['request_id'])
EOF
RID=$(cat /tmp/mm_request_id3); until curl -s localhost:8081/analyze/$RID | grep -q -E '"status":"(failed|completed)"'; do sleep 5; done
```

- [ ] **Step 3: Read the trace back from Langfuse (free)**

```bash
cd /Users/miguel/Projects/market-mind-org/marketmind-orchestrator && RID=$(cat /tmp/mm_request_id3) poetry run python - <<'EOF'
import base64, json, os, urllib.request, collections
from langfuse import Langfuse
from app.config import get_settings
s = get_settings(); tid = Langfuse.create_trace_id(seed=os.environ["RID"])
auth = {"Authorization": "Basic " + base64.b64encode(f"{s.langfuse_public_key}:{s.langfuse_secret_key}".encode()).decode()}
obs = json.load(urllib.request.urlopen(urllib.request.Request(f"{s.langfuse_host.rstrip('/')}/api/public/v2/observations?traceId={tid}&limit=100", headers=auth)))["data"]
gens = [o for o in obs if o.get("type") == "GENERATION"]
print("observations:", len(obs), "| generations:", [(g["name"], g.get("model"), g.get("usageDetails")) for g in gens])
EOF
```

Expected: `generations` includes `enrichment` with a model and non-zero `input`/`output` usage, inside the run's trace. Also check in the Langfuse UI that the `identify-image` call appears as its own trace with a `vision` generation.

---

### Task 7: Push and open the PRs

- [ ] **Step 1: Push both branches**

```bash
cd /Users/miguel/Projects/market-mind-org/marketmind-core && git push -u origin feat/langfuse-llm-tracing
cd ../marketmind-orchestrator && git push -u origin fix/langfuse-scores && git push -u origin feat/langfuse-llm-tracing
```

- [ ] **Step 2: Open the PRs** (check each repo for a PR template first: `.github/pull_request_template.md`)

- Orchestrator: `fix/langfuse-scores` → `master` ("Attach the five measures to the run's trace").
- Orchestrator: `feat/langfuse-llm-tracing` → `fix/langfuse-scores` (stacked), to be retargeted to `master` after the fix merges.
- Core: `feat/langfuse-llm-tracing` → `main`.

Each body covers what changed, why, how it was tested (unit counts, plus the live trace from Task 6 if it ran), and the config needed (`LANGFUSE_*` in core's env). End each body with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`. No model names in PR text.
