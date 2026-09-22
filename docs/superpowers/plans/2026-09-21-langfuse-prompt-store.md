# Prompt store on Langfuse Prompt Management: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Core's six model instructions are edited in Langfuse Prompt Management. Core uses the `production` version, falls back to the in-code text, and every traced generation is linked to the prompt version it used.

**Architecture:**
- `app/prompts.py` fetches a prompt by name, falling back to the code text, and records the fetched version in a context variable. `TracedProvider` hands that version to the generation it opens.
- Call sites swap their constants for `prompt_text(name, constant, **vars)`.
- A registry (`app/prompt_registry.py`) feeds both a startup cache warm-up and `scripts/sync_prompts.py`, which seeds Langfuse from code.

**Tech stack:** Python 3.12+, FastAPI, Langfuse Python SDK v4 (`get_prompt`, `create_prompt`, `TextPromptClient`, `TemplateParser.compile_template`), pytest (`asyncio_mode = "auto"`).

**Spec:** `docs/superpowers/specs/2026-09-21-langfuse-prompt-store-design.md`

## Global constraints

- Prompt names, exactly: `catalog-match-system`, `enrichment-system`, `reference-market-system`, `vision-instructions`, `explanation-system-pt`, `explanation-system-en`.
- Variables: `vision-instructions` takes `subject` and `country_list`. `explanation-system-pt` and `explanation-system-en` take `structure`, `scope_intro`, `country_rule` and `ruler_scope`. The other prompts take none.
- Templates use Langfuse `{{variable}}` syntax, both in Langfuse and in the code defaults. Filling is single-pass `TemplateParser.compile_template`, the function `TextPromptClient.compile` uses.
- Runtime fetch: `get_prompt(name, label="production", type="text", fallback=<code text>, max_retries=1, fetch_timeout_seconds=3)`, with the SDK default cache (60 s, background refresh).
- `prompt_text` never raises. When tracing is unconfigured (`observability.client()` is `None`, as in all tests), it uses the code text. A fallback prompt is never linked.
- While the defaults are in use, the rendered text of every call site is **byte-identical** to before this work (Task 1 pins it).
- **No paid API calls without the user's explicit approval.** All tests use fakes or locally constructed prompt objects. Task 6 (seeding Langfuse, no LLM) is free. Task 7 is paid and must be asked first.
- Branch: `feat/langfuse-prompt-store` in `marketmind-core` (stacked on `feat/langfuse-llm-tracing`). Never commit to `main`.
- Commit messages: a plain imperative sentence, then a blank line and `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- Never print `.env` values. Never commit `poetry.lock`.

## File map

| File | Responsibility |
|---|---|
| `tests/test_prompt_snapshots.py`, `tests/prompt_snapshots.json` (new) | pin today's rendered explanation and vision texts |
| `app/prompts.py` (new) | `prompt_text`, `take_linked_prompt`, `warm`, `LABEL` |
| `tests/test_prompts.py` (new) | prompt store behaviour |
| `app/services/llm/traced.py` | pass the linked prompt to the generation |
| `app/services/{catalog_matcher,enrichment_client,reference_market_client,vision_client,explanation_client}.py` | fetch their instruction by name |
| `tests/test_prompt_call_sites.py` (new) | each site asks for its own name and uses the fetched text |
| `app/prompt_registry.py` (new) | `PROMPTS`: name → code default |
| `app/main.py` | start the warm-up in the lifespan |
| `scripts/sync_prompts.py` (new), `tests/test_sync_prompts.py` (new) | seed and sync Langfuse from code |

## How to run core's tests

```bash
cd /Users/miguel/Projects/market-mind-org/marketmind-core
DATABASE_HOST=localhost DATABASE_PORT=5436 DATABASE_NAME=marketmind_test poetry run pytest <path> -v
```

Full suite: add `-q -p no:warnings`. The baseline before this plan is **464 passed**. `tests/conftest.py` blanks the Langfuse keys, so the prompt store runs on code defaults in every test.

---

### Task 1: Pin today's rendered prompt texts

A characterization test. It passes on today's code by construction, and it must stay green through Task 4's refactor.

**Files:**
- Create: `tests/test_prompt_snapshots.py`, `tests/prompt_snapshots.json`

**Interfaces:**
- Produces: the snapshot test, which Task 4 must keep green.

- [ ] **Step 1: Write the test**

Create `tests/test_prompt_snapshots.py`:

```python
"""The instruction texts the models receive, pinned before they moved to Langfuse.

While the defaults are in use (no Langfuse — as in every test), moving the texts
into Langfuse Prompt Management must not change a byte of what the model sees.
The snapshot was generated from the code before the move."""

import itertools
import json
from functools import lru_cache
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.services.explanation_client import _system_prompt
from app.services.vision_client import ProductIdentification, VisionClient

_SNAPSHOT_FILE = Path(__file__).parent / "prompt_snapshots.json"

_EXPLANATION_CASES = list(
    itertools.product(("pt", "en", "es"), (True, False), (False, True), (None, "Quero vender no Chile"))
)

_VISION_CASES = [
    ("ZmFrZQ==", "image/jpeg", "air fryer 5L", ["BRA", "USA"]),
    ("ZmFrZQ==", "image/jpeg", None, ["BRA", "USA"]),
    (None, None, "air fryer 5L", ["BRA", "USA"]),
    ("ZmFrZQ==", "image/jpeg", "air fryer 5L", []),
    ("ZmFrZQ==", "image/jpeg", None, []),
    (None, None, "air fryer 5L", []),
]


@lru_cache(maxsize=1)
def _snapshot() -> dict[str, str]:
    return json.loads(_SNAPSHOT_FILE.read_text(encoding="utf-8"))


def _explanation_key(language: str, single: bool, champion: bool, user_prompt: str | None) -> str:
    return f"explanation|{language}|single={single}|champion={champion}|user_prompt={user_prompt is not None}"


def _vision_key(image: str | None, hint: str | None, countries: list[str]) -> str:
    return f"vision|image={image is not None}|hint={hint is not None}|countries={','.join(countries)}"


async def _vision_prompt(image: str | None, media_type: str | None, hint: str | None, countries: list[str]) -> str:
    provider = AsyncMock()
    provider.complete = AsyncMock(
        return_value=ProductIdentification(
            product_name="air fryer",
            brand="Philips",
            category="kitchen",
            target_age_min=None,
            target_age_max=None,
            estimated_price=89.9,
            price_currency="USD",
            confidence=0.9,
        )
    )
    with patch("app.services.vision_client.resolve_vision_llm", return_value=provider):
        await VisionClient().identify_product(
            image_base64=image, image_media_type=media_type, hint_text=hint, countries=countries
        )
    return provider.complete.await_args.kwargs["user"]


@pytest.mark.parametrize(("language", "single", "champion", "user_prompt"), _EXPLANATION_CASES)
def test_the_explanation_system_prompt_is_unchanged(language, single, champion, user_prompt) -> None:
    rendered = _system_prompt(language, single_country=single, user_prompt=user_prompt, external_champion=champion)

    assert rendered == _snapshot()[_explanation_key(language, single, champion, user_prompt)]


@pytest.mark.parametrize(("image", "media_type", "hint", "countries"), _VISION_CASES)
async def test_the_vision_instructions_are_unchanged(image, media_type, hint, countries) -> None:
    rendered = await _vision_prompt(image, media_type, hint, countries)

    assert rendered == _snapshot()[_vision_key(image, hint, countries)]
```

- [ ] **Step 2: Generate the snapshot from today's code**

```bash
DATABASE_HOST=localhost DATABASE_PORT=5436 DATABASE_NAME=marketmind_test poetry run python - <<'EOF'
import asyncio, json
from tests.test_prompt_snapshots import (
    _EXPLANATION_CASES, _VISION_CASES, _SNAPSHOT_FILE, _explanation_key, _vision_key, _vision_prompt,
)
from app.services.explanation_client import _system_prompt

snapshot = {}
for language, single, champion, user_prompt in _EXPLANATION_CASES:
    snapshot[_explanation_key(language, single, champion, user_prompt)] = _system_prompt(
        language, single_country=single, user_prompt=user_prompt, external_champion=champion
    )

async def vision():
    for image, media_type, hint, countries in _VISION_CASES:
        snapshot[_vision_key(image, hint, countries)] = await _vision_prompt(image, media_type, hint, countries)

asyncio.run(vision())
_SNAPSHOT_FILE.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
print(len(snapshot), "snapshots written")
EOF
```

Expected: `30 snapshots written`.

- [ ] **Step 3: Run the test**

Run: `... poetry run pytest tests/test_prompt_snapshots.py -v`
Expected: 30 passed. This is a characterization test, so it passes on today's code by design. To prove it detects a change, temporarily edit one character of `_SYSTEM_PROMPT_EN` in `app/services/explanation_client.py`, rerun, and see failures. Then revert the edit with `git checkout -- app/services/explanation_client.py` and confirm 30 pass again.

- [ ] **Step 4: Commit**

```bash
git add tests/test_prompt_snapshots.py tests/prompt_snapshots.json
git commit -m "Pin the instruction texts the models receive today

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: `app/prompts.py`, the prompt store

**Files:**
- Create: `app/prompts.py`, `tests/test_prompts.py`

**Interfaces:**
- Consumes: `observability.client()`, called through the module (tests monkeypatch it).
- Produces:
  - `LABEL = "production"`
  - `prompt_text(name: str, default: str, **variables: str) -> str`
  - `take_linked_prompt() -> Any | None` (read-once)
  - `warm(registry: Mapping[str, str]) -> None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_prompts.py`:

```python
"""The instructions come from Langfuse when it is there, and from code when not."""

import pytest
from langfuse.api import Prompt_Text
from langfuse.model import TextPromptClient

from app import observability, prompts


def _stored(name: str, text: str, *, version: int = 2, fallback: bool = False) -> TextPromptClient:
    return TextPromptClient(
        Prompt_Text(name=name, version=version, prompt=text, labels=["production"], tags=[], config={}),
        is_fallback=fallback,
    )


class FakeLangfuse:
    """Stands in for the Langfuse client: the network is the only thing faked."""

    def __init__(self, result: TextPromptClient | None = None, error: Exception | None = None) -> None:
        self.result, self.error = result, error
        self.requests: list[tuple[str, dict]] = []

    def get_prompt(self, name: str, **options: object) -> TextPromptClient | None:
        self.requests.append((name, options))
        if self.error:
            raise self.error
        return self.result


@pytest.fixture(autouse=True)
def _no_link_left_behind() -> None:
    prompts.take_linked_prompt()
    yield
    prompts.take_linked_prompt()


def test_without_langfuse_the_code_text_is_used_and_filled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(observability, "client", lambda: None)

    text = prompts.prompt_text(
        "vision-instructions", "Identify {{subject}} in {{country_list}}.", subject="this product", country_list="BRA"
    )

    assert text == "Identify this product in BRA."
    assert prompts.take_linked_prompt() is None


def test_a_text_without_variables_comes_back_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(observability, "client", lambda: None)
    text = "Rules:\n- keep {single braces} literally\n"

    assert prompts.prompt_text("enrichment-system", text) == text


def test_the_production_version_replaces_the_code_text_and_is_linked(monkeypatch: pytest.MonkeyPatch) -> None:
    stored = _stored("enrichment-system", "Edited in Langfuse for {{who}}.", version=4)
    fake = FakeLangfuse(result=stored)
    monkeypatch.setattr(observability, "client", lambda: fake)

    text = prompts.prompt_text("enrichment-system", "the code text", who="Chile")

    assert text == "Edited in Langfuse for Chile."
    name, options = fake.requests[0]
    assert name == "enrichment-system"
    assert options["label"] == "production"
    assert options["type"] == "text"
    assert options["fallback"] == "the code text"
    assert options["max_retries"] == 1
    assert options["fetch_timeout_seconds"] == 3
    assert prompts.take_linked_prompt() is stored


def test_a_fallback_is_used_but_never_linked(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeLangfuse(result=_stored("enrichment-system", "the code text", fallback=True))
    monkeypatch.setattr(observability, "client", lambda: fake)

    assert prompts.prompt_text("enrichment-system", "the code text") == "the code text"
    assert prompts.take_linked_prompt() is None


def test_a_failing_fetch_uses_the_code_text_and_clears_a_stale_link(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(observability, "client", lambda: FakeLangfuse(result=_stored("catalog-match-system", "x")))
    prompts.prompt_text("catalog-match-system", "x")  # fetched, but its call never happened
    monkeypatch.setattr(observability, "client", lambda: FakeLangfuse(error=RuntimeError("langfuse down")))

    text = prompts.prompt_text("reference-market-system", "the {{kind}} text", kind="code")

    assert text == "the code text"
    assert prompts.take_linked_prompt() is None


def test_a_fallback_clears_a_stale_link(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(observability, "client", lambda: FakeLangfuse(result=_stored("enrichment-system", "x")))
    prompts.prompt_text("enrichment-system", "x")
    monkeypatch.setattr(
        observability, "client", lambda: FakeLangfuse(result=_stored("vision-instructions", "y", fallback=True))
    )

    prompts.prompt_text("vision-instructions", "y")

    assert prompts.take_linked_prompt() is None


def test_a_linked_prompt_is_handed_over_once(monkeypatch: pytest.MonkeyPatch) -> None:
    stored = _stored("catalog-match-system", "x")
    monkeypatch.setattr(observability, "client", lambda: FakeLangfuse(result=stored))

    prompts.prompt_text("catalog-match-system", "x")

    assert prompts.take_linked_prompt() is stored
    assert prompts.take_linked_prompt() is None


def test_warming_fetches_every_prompt_once(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeLangfuse(result=_stored("any", "text"))
    monkeypatch.setattr(observability, "client", lambda: fake)

    prompts.warm({"catalog-match-system": "a", "vision-instructions": "b {{subject}}"})

    assert [name for name, _ in fake.requests] == ["catalog-match-system", "vision-instructions"]
    assert prompts.take_linked_prompt() is None, "warming must not leave a link for the next real call"
```

- [ ] **Step 2: Run them to confirm they fail**

Run: `... poetry run pytest tests/test_prompts.py -v`
Expected: FAIL (`ImportError: cannot import name 'prompts' from 'app'`).

- [ ] **Step 3: Implement `app/prompts.py`**

```python
"""The instructions core gives its models, kept in Langfuse Prompt Management.

Langfuse is where they are edited: the version labelled `production` is the one
used, and a new one takes effect on the next call (the SDK caches for 60 s and
refreshes in the background) without a deploy. The text in code is the
fallback — for tests, for an unconfigured service, for when Langfuse is down —
and the seed for Langfuse (scripts/sync_prompts.py), not a second copy kept in
sync by hand.

Which version a call used is handed to the traced generation of that call
through a context variable: the same idiom as the token tally
(app/services/llm/usage.py), so the providers stay pure transport. Each call
site fetches its prompt immediately before its model call, in the same task.

Never raises: a prompt that cannot be fetched is the text in code.
"""

import logging
from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any

from langfuse.model import TemplateParser

from app import observability

logger = logging.getLogger("marketmind-scoring.prompts")

LABEL = "production"
_FETCH_TIMEOUT_SECONDS = 3

_linked: ContextVar[Any | None] = ContextVar("linked_prompt", default=None)


def prompt_text(name: str, default: str, **variables: str) -> str:
    """The instruction called `name`, with its variables filled in."""
    client = observability.client()
    if client is None:
        return TemplateParser.compile_template(default, variables)
    try:
        stored = client.get_prompt(
            name,
            label=LABEL,
            type="text",
            fallback=default,
            max_retries=1,
            fetch_timeout_seconds=_FETCH_TIMEOUT_SECONDS,
        )
        text = stored.compile(**variables)
    except Exception:  # noqa: BLE001 — a prompt that cannot be fetched is the text in code
        logger.debug("Could not fetch prompt %s; using the text in code", name, exc_info=True)
        _linked.set(None)
        return TemplateParser.compile_template(default, variables)
    # A fallback is the code text wearing a prompt's clothes: Langfuse cannot
    # link a generation to it, and it must not inherit a stale link either.
    _linked.set(None if stored.is_fallback else stored)
    return text


def take_linked_prompt() -> Any | None:
    """The prompt fetched for the next generation, handed over once."""
    stored = _linked.get()
    _linked.set(None)
    return stored


def warm(registry: Mapping[str, str]) -> None:
    """Fetch every prompt once, so no request pays the first fetch.

    get_prompt is a synchronous HTTP call; inside a request it would block the
    event loop for as long as Langfuse takes to answer. Run from a thread at
    startup, the fetches land in the SDK's cache, which then refreshes itself
    in the background.
    """
    for name, default in registry.items():
        prompt_text(name, default)
    take_linked_prompt()
    logger.info("Warmed %d prompts from Langfuse", len(registry))
```

- [ ] **Step 4: Run the tests to confirm they pass**

Run: `... poetry run pytest tests/test_prompts.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add app/prompts.py tests/test_prompts.py
git commit -m "Fetch instructions from Langfuse with the code text as fallback

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Link each generation to the prompt version it used

**Files:**
- Modify: `app/services/llm/traced.py`: the import line (`from app import observability`, line 17) and `TracedProvider._start` (lines 55–72)
- Test: `tests/test_llm_tracing.py` (append two tests and add imports at the top)

**Interfaces:**
- Consumes: `prompts.prompt_text`, `prompts.take_linked_prompt` (Task 2).
- Produces: generations carrying the attributes `langfuse.observation.prompt.name` and `.version`.

- [ ] **Step 1: Write the failing tests**

Add to the top-of-file imports of `tests/test_llm_tracing.py`:

```python
from langfuse.api import Prompt_Text
from langfuse.model import TextPromptClient

from app import prompts
```

(`from app import observability` is already imported; merge rather than duplicate.)

Append:

```python
async def test_a_generation_is_linked_to_the_prompt_version_it_used(spans, monkeypatch: pytest.MonkeyPatch) -> None:
    prompts.take_linked_prompt()
    stored = TextPromptClient(
        Prompt_Text(name="enrichment-system", version=4, prompt="You localise.", labels=["production"], tags=[], config={})
    )
    monkeypatch.setattr(observability.client(), "get_prompt", lambda name, **_: stored)

    system = prompts.prompt_text("enrichment-system", "the code text")
    await TracedProvider(FakeProvider(), site="enrichment").complete(system=system, user="x")

    gen = spans()["enrichment"]
    assert _attr(gen, "prompt.name") == "enrichment-system"
    assert _attr(gen, "prompt.version") == 4
    assert json.loads(_attr(gen, "input"))["system"] == "You localise."


async def test_a_call_without_a_fetched_prompt_is_not_linked(spans) -> None:
    prompts.take_linked_prompt()

    await TracedProvider(FakeProvider(), site="explanation").complete(user="x")

    assert _attr(spans()["explanation"], "prompt.name") is None
```

- [ ] **Step 2: Run them to confirm they fail**

Run: `... poetry run pytest tests/test_llm_tracing.py -v -k "linked"`
Expected: `test_a_generation_is_linked_to_the_prompt_version_it_used` FAILS on `prompt.name` being `None`. The other test passes.

- [ ] **Step 3: Implement**

In `app/services/llm/traced.py`, change the import `from app import observability` to:

```python
from app import observability, prompts
```

In `_start`, replace the `return client.start_observation(...)` call with:

```python
            linked = prompts.take_linked_prompt()
            return client.start_observation(
                name=self.site,
                as_type="generation",
                model=self.model,
                input=recorded,
                metadata={"provider": type(self.inner).__name__},
                **({"prompt": linked} if linked is not None else {}),
            )
```

This stays inside the existing `try`, so linking can never fail a call.

- [ ] **Step 4: Run the tests and the full suite**

Run: `... poetry run pytest tests/test_llm_tracing.py tests/test_prompts.py -v`
Expected: all pass.

Run the full suite and confirm the previous count plus the new tests, with no failures.

- [ ] **Step 5: Commit**

```bash
git add app/services/llm/traced.py tests/test_llm_tracing.py
git commit -m "Link each generation to the prompt version it used

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Each call site fetches its instruction by name

**Files:**
- Modify: `app/services/catalog_matcher.py` (imports around line 37; `system=_SYSTEM` around line 148)
- Modify: `app/services/enrichment_client.py` (imports around line 15; line 112)
- Modify: `app/services/reference_market_client.py` (imports around line 19; lines 76–77)
- Modify: `app/services/vision_client.py` (imports around line 18; new module constant; lines 145–189)
- Modify: `app/services/explanation_client.py` (imports around line 29; `_SYSTEM_PROMPT_PT` line 116 and `_SYSTEM_PROMPT_EN` line 165; `_system_prompt` lines 372–390)
- Create: `tests/test_prompt_call_sites.py`

**Interfaces:**
- Consumes: `prompt_text` (Task 2).
- Produces: module constants `vision_client._INSTRUCTIONS` and `explanation_client._SYSTEM_PROMPT_PT` / `_EN` in `{{var}}` syntax. Task 5's registry imports them, together with the three `_SYSTEM` constants.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_prompt_call_sites.py`:

```python
"""Each call site asks Langfuse for its own instruction, by the name in the spec,
and sends the model what came back."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from langfuse.api import Prompt_Text
from langfuse.model import TextPromptClient

from app import observability, prompts
from app.services.catalog_matcher import CatalogMatchDecision, CatalogMatcher
from app.services.enrichment_client import EnrichmentClient, LocalizedMarketsResult
from app.services.explanation_client import _system_prompt
from app.services.reference_market_client import ReferenceCandidatesResult, ReferenceMarketClient
from app.services.vision_client import MarketTerms, ProductIdentification, VisionClient


class EditedInLangfuse:
    """Stands in for Langfuse: every prompt comes back as 'EDITED <name>'."""

    def __init__(self) -> None:
        self.names: list[str] = []

    def get_prompt(self, name: str, **_: object) -> TextPromptClient:
        self.names.append(name)
        return TextPromptClient(
            Prompt_Text(name=name, version=7, prompt=f"EDITED {name}", labels=["production"], tags=[], config={})
        )


@pytest.fixture
def langfuse(monkeypatch: pytest.MonkeyPatch) -> EditedInLangfuse:
    fake = EditedInLangfuse()
    monkeypatch.setattr(observability, "client", lambda: fake)
    yield fake
    prompts.take_linked_prompt()


class _Capture:
    def __init__(self, reply: object) -> None:
        self.reply = reply
        self.system: str | None = None
        self.user: str | None = None

    async def complete(self, *, system=None, user, image=None, response_model=None):
        self.system, self.user = system, user
        return self.reply


class _Product:
    def __init__(self, title: str) -> None:
        self.entity_key = uuid.uuid4()
        self.canonical_title = title
        self.category = None
        self.localized_markets = None


async def test_catalog_match_uses_its_prompt(langfuse: EditedInLangfuse) -> None:
    air = _Product("air fryer")
    provider = _Capture(CatalogMatchDecision(entity_key=str(air.entity_key), reasoning="same appliance"))

    await CatalogMatcher(provider=provider).pick(text="freidora de aire", candidates=[air])

    assert provider.system == "EDITED catalog-match-system"
    assert langfuse.names == ["catalog-match-system"]


async def test_enrichment_uses_its_prompt(langfuse: EditedInLangfuse) -> None:
    provider = AsyncMock()
    provider.complete = AsyncMock(
        return_value=LocalizedMarketsResult(
            markets=[MarketTerms(country="BRA", primary_name="fritadeira", search_terms=["fritadeira"], confidence=0.9)]
        )
    )
    with patch("app.services.enrichment_client.resolve_enrichment_llm", return_value=provider):
        client = EnrichmentClient()

    await client.localized_markets(product_name="air fryer", brand=None, category="kitchen", countries=[("BRA", "pt")])

    assert provider.complete.await_args.kwargs["system"] == "EDITED enrichment-system"


async def test_reference_market_uses_its_prompt(langfuse: EditedInLangfuse) -> None:
    provider = AsyncMock()
    provider.complete = AsyncMock(
        return_value=ReferenceCandidatesResult(
            candidates=[
                MarketTerms(country="BRA", primary_name="fritadeira", search_terms=["air fryer"], confidence=0.95),
                MarketTerms(country="USA", primary_name="air fryer", search_terms=["air fryer"], confidence=0.9),
            ]
        )
    )
    with patch("app.services.reference_market_client.resolve_reference_market_llm", return_value=provider):
        client = ReferenceMarketClient()

    await client.candidates(
        product_name="air fryer", brand=None, category="kitchen", supported_countries=["USA", "BRA", "DEU"]
    )

    assert provider.complete.await_args.kwargs["system"] == "EDITED reference-market-system"


async def test_vision_uses_its_prompt_and_keeps_the_users_text(langfuse: EditedInLangfuse) -> None:
    provider = AsyncMock()
    provider.complete = AsyncMock(
        return_value=ProductIdentification(
            product_name="air fryer",
            brand=None,
            category="kitchen",
            target_age_min=None,
            target_age_max=None,
            estimated_price=None,
            price_currency=None,
            confidence=0.9,
        )
    )
    with patch("app.services.vision_client.resolve_vision_llm", return_value=provider):
        await VisionClient().identify_product(
            image_base64=None, image_media_type=None, hint_text="air fryer 5L", countries=["BRA"]
        )

    assert provider.complete.await_args.kwargs["user"] == (
        "EDITED vision-instructions\n\nProduct description from the user: air fryer 5L"
    )


@pytest.mark.parametrize(
    ("language", "name"),
    [("pt", "explanation-system-pt"), ("en", "explanation-system-en"), ("es", "explanation-system-en")],
)
def test_explanation_uses_its_prompt_per_language(langfuse: EditedInLangfuse, language: str, name: str) -> None:
    rendered = _system_prompt(language, single_country=True)

    assert rendered.startswith(f"EDITED {name}")
    assert langfuse.names == [name]
```

- [ ] **Step 2: Run them to confirm they fail**

Run: `... poetry run pytest tests/test_prompt_call_sites.py -v`
Expected: all 7 FAIL. The call sites still send the in-code text, and `get_prompt` is never called.

- [ ] **Step 3: Switch the three fixed system prompts**

In each of these files, add `from app.prompts import prompt_text` to the `app.` imports:
- In `app/services/catalog_matcher.py`, replace `system=_SYSTEM` with `system=prompt_text("catalog-match-system", _SYSTEM)`.
- In `app/services/enrichment_client.py`, replace `system=_SYSTEM` with `system=prompt_text("enrichment-system", _SYSTEM)`.
- In `app/services/reference_market_client.py`, replace `system=_SYSTEM` with `system=prompt_text("reference-market-system", _SYSTEM)`.

- [ ] **Step 4: Move the vision instructions into a template constant**

In `app/services/vision_client.py`, add `from app.prompts import prompt_text` to the imports.

**Cut** the parenthesized string expression assigned to `instructions` inside `identify_product`: everything from `instructions = (` through its closing `)`, currently lines 145–189. **Paste** it at module level directly above `class VisionClient`, renamed `_INSTRUCTIONS = (`, at column 0 with the lines re-indented by one level less. Change exactly two lines of the pasted text and **no other character**:
- `f"Identify {subject} for a cross-country market-demand analysis. "` becomes `"Identify {{subject}} for a cross-country market-demand analysis. "`
- `f"{country_list}. For each country, `search_terms` is a GENEROUS list of the real terms a local "` becomes `"{{country_list}}. For each country, `search_terms` is a GENEROUS list of the real terms a local "`

Add a comment above the constant:

```python
# The instructions for the vision call, edited in Langfuse ("vision-instructions");
# this is the fallback and the seed. {{subject}} and {{country_list}} are filled per call.
```

Where the block was cut, `identify_product` now reads:

```python
        instructions = prompt_text(
            "vision-instructions", _INSTRUCTIONS, subject=subject, country_list=country_list
        )
```

The `country_list =` and `subject =` lines above it, and the `hint_text` / `label` lines below it, are unchanged.

- [ ] **Step 5: Explanation base templates**

In `app/services/explanation_client.py`, add `from app.prompts import prompt_text` to the imports.

In `_SYSTEM_PROMPT_PT` and `_SYSTEM_PROMPT_EN`, and only there, replace each `{structure}`, `{scope_intro}`, `{country_rule}` and `{ruler_scope}` with `{{structure}}`, `{{scope_intro}}`, `{{country_rule}}` and `{{ruler_scope}}`. Each appears once per template. `_USER_PROMPT_BLOCK_PT` / `_EN` keep their `{user_prompt}`, which is still filled with `.replace` in code.

In `_system_prompt`, replace the two `base = (...)` assignments (the `.replace` chains) with:

```python
    if is_pt:
        structure = _STRUCTURE_PT_SINGLE if single_country else _STRUCTURE_PT_MULTI
        base = prompt_text(
            "explanation-system-pt",
            _SYSTEM_PROMPT_PT,
            structure=structure,
            scope_intro=_SCOPE_INTRO_PT[single_country],
            country_rule=_COUNTRY_RULE_PT[single_country],
            ruler_scope=_RULER_SCOPE_PT[single_country],
        )
    else:
        structure = _STRUCTURE_EN_SINGLE if single_country else _STRUCTURE_EN_MULTI
        base = prompt_text(
            "explanation-system-en",
            _SYSTEM_PROMPT_EN,
            structure=structure,
            scope_intro=_SCOPE_INTRO_EN[single_country],
            country_rule=_COUNTRY_RULE_EN[single_country],
            ruler_scope=_RULER_SCOPE_EN[single_country],
        )
        if lang not in ("en", "en-us", "en_us", "english"):
            base = f'{base}\n\nWrite the entire narrative in this language: "{language}".'
```

Everything after that (the demand-confidence and demand-terms rules, the champion rule, and the user-question block) is unchanged.

- [ ] **Step 6: Run the call-site tests, the snapshots, the existing prompt tests, and the full suite**

Run: `... poetry run pytest tests/test_prompt_call_sites.py tests/test_prompt_snapshots.py tests/test_explanation_prompt.py tests/test_vision_client.py tests/test_catalog_matcher.py tests/test_enrichment_client.py tests/test_reference_market_client.py -v`
Expected: all pass. The 30 snapshots must pass **unchanged**: the snapshot file is never edited in this task.

Run the full suite and confirm no failures. Run `poetry run ruff check app tests` and confirm no new findings.

- [ ] **Step 7: Commit**

```bash
git add app/services/catalog_matcher.py app/services/enrichment_client.py app/services/reference_market_client.py app/services/vision_client.py app/services/explanation_client.py tests/test_prompt_call_sites.py
git commit -m "Fetch each instruction from Langfuse by name at its call site

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Registry, startup warm-up and the sync script

**Files:**
- Create: `app/prompt_registry.py`, `scripts/sync_prompts.py`, `tests/test_sync_prompts.py`
- Modify: `app/main.py` (the `lifespan` function, around lines 26–38)

**Interfaces:**
- Consumes:
  - `prompts.warm`, `prompts.LABEL`, `observability.client()`, `observability.tracing_enabled()`
  - the constants from Task 4: `catalog_matcher._SYSTEM`, `enrichment_client._SYSTEM`, `reference_market_client._SYSTEM`, `vision_client._INSTRUCTIONS`, `explanation_client._SYSTEM_PROMPT_PT`, `explanation_client._SYSTEM_PROMPT_EN`
- Produces:
  - `PROMPTS: dict[str, str]`
  - `scripts.sync_prompts.sync(client, registry, *, update: bool) -> list[str]`
  - `scripts.sync_prompts.main(argv: list[str] | None = None) -> int`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_sync_prompts.py`:

```python
"""Langfuse is seeded from the texts in code, and never overwritten by accident."""

import importlib.util
from pathlib import Path

from langfuse.api import NotFoundError, Prompt_Text
from langfuse.model import TextPromptClient

from app.prompt_registry import PROMPTS
from app.services import catalog_matcher, enrichment_client, explanation_client, reference_market_client, vision_client

_spec = importlib.util.spec_from_file_location(
    "sync_prompts", Path(__file__).resolve().parents[1] / "scripts" / "sync_prompts.py"
)
sync_prompts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync_prompts)


class FakeLangfuse:
    """Stands in for Langfuse Prompt Management: the network is the only thing faked."""

    def __init__(self, existing: dict[str, str] | None = None) -> None:
        self.existing = dict(existing or {})
        self.created: list[dict] = []

    def get_prompt(self, name: str, **_: object) -> TextPromptClient:
        if name not in self.existing:
            raise NotFoundError(body={"message": "Prompt not found"})
        return TextPromptClient(
            Prompt_Text(name=name, version=3, prompt=self.existing[name], labels=["production"], tags=[], config={})
        )

    def create_prompt(self, **prompt: object) -> None:
        self.created.append(prompt)
        self.existing[prompt["name"]] = prompt["prompt"]


def test_the_registry_holds_the_six_instructions_from_code() -> None:
    assert PROMPTS == {
        "catalog-match-system": catalog_matcher._SYSTEM,
        "enrichment-system": enrichment_client._SYSTEM,
        "reference-market-system": reference_market_client._SYSTEM,
        "vision-instructions": vision_client._INSTRUCTIONS,
        "explanation-system-pt": explanation_client._SYSTEM_PROMPT_PT,
        "explanation-system-en": explanation_client._SYSTEM_PROMPT_EN,
    }


def test_seeding_creates_only_what_is_missing() -> None:
    fake = FakeLangfuse(existing={"a": "edited in the UI"})

    report = sync_prompts.sync(fake, {"a": "code a", "b": "code b"}, update=False)

    assert [c["name"] for c in fake.created] == ["b"]
    assert fake.created[0]["prompt"] == "code b"
    assert fake.created[0]["labels"] == ["production"]
    assert fake.created[0]["type"] == "text"
    assert fake.existing["a"] == "edited in the UI", "seeding never overwrites"
    assert report == ["a: exists v3 (differs from code)", "b: created"]


def test_update_pushes_a_new_version_only_where_the_text_differs() -> None:
    fake = FakeLangfuse(existing={"a": "edited in the UI", "b": "code b"})

    report = sync_prompts.sync(fake, {"a": "code a", "b": "code b"}, update=True)

    assert [(c["name"], c["prompt"]) for c in fake.created] == [("a", "code a")]
    assert report == ["a: updated (was v3)", "b: exists v3"]


def test_the_script_refuses_to_run_without_langfuse(monkeypatch) -> None:
    monkeypatch.setattr(sync_prompts.observability, "client", lambda: None)

    assert sync_prompts.main([]) == 1
```

- [ ] **Step 2: Run them to confirm they fail**

Run: `... poetry run pytest tests/test_sync_prompts.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'app.prompt_registry'`).

- [ ] **Step 3: Implement the registry**

Create `app/prompt_registry.py`:

```python
"""Every instruction kept in Langfuse Prompt Management, by name, with its text
in code — the fallback at runtime and the seed for scripts/sync_prompts.py.

Its own module so that app/prompts.py never imports the service modules."""

from app.services.catalog_matcher import _SYSTEM as _CATALOG_MATCH_SYSTEM
from app.services.enrichment_client import _SYSTEM as _ENRICHMENT_SYSTEM
from app.services.explanation_client import _SYSTEM_PROMPT_EN, _SYSTEM_PROMPT_PT
from app.services.reference_market_client import _SYSTEM as _REFERENCE_MARKET_SYSTEM
from app.services.vision_client import _INSTRUCTIONS as _VISION_INSTRUCTIONS

PROMPTS: dict[str, str] = {
    "catalog-match-system": _CATALOG_MATCH_SYSTEM,
    "enrichment-system": _ENRICHMENT_SYSTEM,
    "reference-market-system": _REFERENCE_MARKET_SYSTEM,
    "vision-instructions": _VISION_INSTRUCTIONS,
    "explanation-system-pt": _SYSTEM_PROMPT_PT,
    "explanation-system-en": _SYSTEM_PROMPT_EN,
}
```

- [ ] **Step 4: Implement the sync script**

Create `scripts/sync_prompts.py`:

```python
"""Seed Langfuse Prompt Management from the instruction texts in code.

Makes no model call — it costs nothing but a few requests to Langfuse.

    python scripts/sync_prompts.py            # create what is missing, touch nothing else
    python scripts/sync_prompts.py --update   # also push the code text where production differs

Langfuse is where the prompts are edited, so the default never overwrites.
--update is for when the code text is the one that moved on: it creates a NEW
version labelled production; the one it replaces stays in Langfuse and can be
promoted back from the UI.
"""

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langfuse.api import NotFoundError  # noqa: E402

from app import observability  # noqa: E402
from app.prompt_registry import PROMPTS  # noqa: E402
from app.prompts import LABEL  # noqa: E402


def sync(client: Any, registry: dict[str, str], *, update: bool) -> list[str]:
    report: list[str] = []
    for name, code_text in registry.items():
        try:
            current = client.get_prompt(name, label=LABEL, type="text", cache_ttl_seconds=0, max_retries=0)
        except NotFoundError:
            client.create_prompt(
                name=name, prompt=code_text, labels=[LABEL], type="text", commit_message="Seeded from code"
            )
            report.append(f"{name}: created")
            continue
        if current.prompt == code_text:
            report.append(f"{name}: exists v{current.version}")
        elif update:
            client.create_prompt(
                name=name, prompt=code_text, labels=[LABEL], type="text", commit_message="Synced from code"
            )
            report.append(f"{name}: updated (was v{current.version})")
        else:
            report.append(f"{name}: exists v{current.version} (differs from code)")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed Langfuse Prompt Management from the texts in code.")
    parser.add_argument(
        "--update", action="store_true", help="push the code text as a new production version where it differs"
    )
    args = parser.parse_args(argv)
    client = observability.client()
    if client is None:
        print("Langfuse is not configured: set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY.", file=sys.stderr)
        return 1
    for line in sync(client, PROMPTS, update=args.update):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Start the warm-up in the lifespan**

In `app/main.py`, add `from app import prompts` next to the existing `from app import observability`. Then in `lifespan`, directly after the existing `if not observability.tracing_enabled(): logger.warning(...)` block, add:

```python
    # get_prompt is a synchronous HTTP call; warmed from a thread here, no
    # request pays for the first fetch on the event loop. Not awaited: startup
    # never waits on Langfuse, and a request that arrives first fetches itself.
    aquecimento = None
    if observability.tracing_enabled():
        from app.prompt_registry import PROMPTS

        aquecimento = asyncio.create_task(asyncio.to_thread(prompts.warm, PROMPTS))
```

Then, after `yield`, next to the existing `varredura.cancel()` teardown, add:

```python
    if aquecimento is not None:
        aquecimento.cancel()
        with suppress(asyncio.CancelledError):
            await aquecimento
```

(`asyncio` and `suppress` are already imported in `app/main.py`.)

- [ ] **Step 6: Run the tests and the full suite**

Run: `... poetry run pytest tests/test_sync_prompts.py tests/test_prompts.py -v`
Expected: all pass.

Run the full suite and confirm no failures. Then run `poetry run ruff check app tests scripts/sync_prompts.py`.

- [ ] **Step 7: Commit**

```bash
git add app/prompt_registry.py scripts/sync_prompts.py tests/test_sync_prompts.py app/main.py
git commit -m "Seed Langfuse from the code texts and warm the prompt cache at startup

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: Seed Langfuse and deploy locally (free: no model calls)

- [ ] **Step 1: Rebuild core with this branch**

```bash
cd /Users/miguel/Projects/market-mind-org/marketmind-core
git branch --show-current   # feat/langfuse-prompt-store
docker compose up -d --build marketmind-scoring-api
until curl -sf localhost:8004/health >/dev/null; do sleep 2; done
```

- [ ] **Step 2: Seed the six prompts (writes to the Langfuse project; no LLM)**

```bash
docker exec marketmind-scoring-api python scripts/sync_prompts.py
```

Expected: six `<name>: created` lines. On a rerun, six `exists v1` lines.

- [ ] **Step 3: Confirm in Langfuse and in the startup log**

```bash
docker compose restart marketmind-scoring-api && until curl -sf localhost:8004/health >/dev/null; do sleep 2; done
docker logs marketmind-scoring-api 2>&1 | grep -c "Warmed 6 of 6 prompts"   # expect 1
cd ../marketmind-orchestrator && poetry run python - <<'EOF'
import base64, json, urllib.request
from app.config import get_settings
s = get_settings()
auth = {"Authorization": "Basic " + base64.b64encode(f"{s.langfuse_public_key}:{s.langfuse_secret_key}".encode()).decode()}
r = json.load(urllib.request.urlopen(urllib.request.Request(f"{s.langfuse_host.rstrip('/')}/api/public/v2/prompts?limit=50", headers=auth)))
for p in r["data"]:
    print(p["name"], p.get("versions"), p.get("labels"))
EOF
```

Expected: the six names, each with version `[1]` and label `production`.

---

### Task 7: Live check. **Paid, so ask the user first.**

- [ ] **Step 1: Ask, stating the cost**

> "The live check runs one analysis for a new product: identify (`vision`), then `catalog_match`, `enrichment` and `reference_market`. That's about 3–4 Claude Opus 5 calls, roughly US$0.15, with DataForSEO in sandbox. It confirms that each generation in Langfuse shows its prompt name and version 1. Optionally, you edit `reference-market-system` in the Langfuse UI and promote it to `production`, and a second run (about US$0.08) shows version 2 linked with no deploy. OK?"

- [ ] **Step 2: Run it** (the same flow as the tracing plan's live check, with a new product and country)
- [ ] **Step 3: Read the generations back** with `fields=core,basic,usage,model,prompt` (or `io`), and confirm each shows `promptName` / `promptVersion`.

---

### Task 8: Push and open the PR

- [ ] `git push -u origin feat/langfuse-prompt-store`
- [ ] Open a PR with base `feat/langfuse-llm-tracing` (stacked on core PR #1), to be retargeted to `main` after #1 merges. The body covers: what, why, how, the six prompt names, how to seed (`python scripts/sync_prompts.py`), `--update` semantics, the test counts, and the live result. End it with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`, and put no model names in the text.
