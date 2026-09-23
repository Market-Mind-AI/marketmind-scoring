"""The instructions come from Langfuse when it is there, and from code when
not — and never from a Langfuse fetch on the request path for a name Langfuse
hasn't confirmed it holds (the known-set design, F1 of the final review)."""

import logging

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


# --- No client configured -----------------------------------------------


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


# --- The known set (F1) --------------------------------------------------


def test_an_unknown_name_uses_the_code_text_with_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """A client exists, but Langfuse has never confirmed this name: no fetch
    on the request path, cold or down."""
    fake = FakeLangfuse(result=_stored("enrichment-system", "Edited in Langfuse."))
    monkeypatch.setattr(observability, "client", lambda: fake)

    text = prompts.prompt_text("enrichment-system", "the code text")

    assert text == "the code text"
    assert fake.requests == []
    assert prompts.take_linked_prompt() is None


def test_a_known_name_is_fetched_inline_with_no_retries_and_linked(monkeypatch: pytest.MonkeyPatch) -> None:
    default = "the code text for {{who}}"
    stored = _stored("enrichment-system", "Edited in Langfuse for {{who}}.", version=4)
    fake = FakeLangfuse(result=stored)
    monkeypatch.setattr(observability, "client", lambda: fake)
    prompts.warm({"enrichment-system": default})
    fake.requests.clear()

    text = prompts.prompt_text("enrichment-system", default, who="Chile")

    assert text == "Edited in Langfuse for Chile."
    name, options = fake.requests[0]
    assert name == "enrichment-system"
    assert options["label"] == "production"
    assert options["type"] == "text"
    assert options["fallback"] == default
    assert options["max_retries"] == 0
    assert options["fetch_timeout_seconds"] == 3
    assert prompts.take_linked_prompt() is stored


def test_an_inline_fallback_discards_the_name_and_the_next_call_makes_no_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeLangfuse(result=_stored("enrichment-system", "the code text"))
    monkeypatch.setattr(observability, "client", lambda: fake)
    prompts.warm({"enrichment-system": "the code text"})

    fake.result = _stored("enrichment-system", "the code text", fallback=True)
    text = prompts.prompt_text("enrichment-system", "the code text")

    assert text == "the code text"
    assert prompts.take_linked_prompt() is None

    fake.requests.clear()
    prompts.prompt_text("enrichment-system", "the code text")
    assert fake.requests == [], "a discarded name must not be fetched again"


def test_a_failing_fetch_discards_the_name_and_the_next_call_makes_no_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeLangfuse(result=_stored("catalog-match-system", "x"))
    monkeypatch.setattr(observability, "client", lambda: fake)
    prompts.warm({"catalog-match-system": "x"})

    fake.error = RuntimeError("langfuse down")
    text = prompts.prompt_text("catalog-match-system", "x")

    assert text == "x"
    assert prompts.take_linked_prompt() is None

    fake.requests.clear()
    prompts.prompt_text("catalog-match-system", "x")
    assert fake.requests == [], "a discarded name must not be fetched again"


def test_a_fallback_clears_a_stale_link(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeLangfuse(result=_stored("enrichment-system", "x"))
    monkeypatch.setattr(observability, "client", lambda: fake)
    prompts.warm({"enrichment-system": "x", "vision-instructions": "y"})
    prompts.prompt_text("enrichment-system", "x")  # fetched, but its call never happened

    fake.result = _stored("vision-instructions", "y", fallback=True)
    prompts.prompt_text("vision-instructions", "y")

    assert prompts.take_linked_prompt() is None


def test_a_failing_fetch_clears_a_stale_link(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeLangfuse(result=_stored("catalog-match-system", "x"))
    monkeypatch.setattr(observability, "client", lambda: fake)
    prompts.warm({"catalog-match-system": "x", "reference-market-system": "the {{kind}} text"})
    prompts.prompt_text("catalog-match-system", "x")  # fetched, but its call never happened

    fake.error = RuntimeError("langfuse down")
    text = prompts.prompt_text("reference-market-system", "the {{kind}} text", kind="code")

    assert text == "the code text"
    assert prompts.take_linked_prompt() is None


def test_a_linked_prompt_is_handed_over_once(monkeypatch: pytest.MonkeyPatch) -> None:
    stored = _stored("catalog-match-system", "x")
    fake = FakeLangfuse(result=stored)
    monkeypatch.setattr(observability, "client", lambda: fake)
    prompts.warm({"catalog-match-system": "x"})

    prompts.prompt_text("catalog-match-system", "x")

    assert prompts.take_linked_prompt() is stored
    assert prompts.take_linked_prompt() is None


# --- warm() ---------------------------------------------------------------


def test_warm_returns_zero_and_does_nothing_without_a_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(observability, "client", lambda: None)

    assert prompts.warm({"catalog-match-system": "a"}) == 0


def test_warm_adds_only_non_fallback_successes_and_survives_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    class SelectiveLangfuse:
        def __init__(self) -> None:
            self.requests: list[str] = []

        def get_prompt(self, name: str, **_: object) -> TextPromptClient:
            self.requests.append(name)
            if name == "enrichment-system":
                return _stored(name, "ok")
            if name == "vision-instructions":
                return _stored(name, "ok", fallback=True)
            raise RuntimeError("langfuse down")

    fake = SelectiveLangfuse()
    monkeypatch.setattr(observability, "client", lambda: fake)
    registry = {"enrichment-system": "a", "vision-instructions": "b", "catalog-match-system": "c"}

    loaded = prompts.warm(registry)

    assert loaded == 1
    assert sorted(fake.requests) == ["catalog-match-system", "enrichment-system", "vision-instructions"]

    fake.requests.clear()
    prompts.warm(registry)

    assert fake.requests == [
        "vision-instructions",
        "catalog-match-system",
    ], "a name already known (enrichment-system) is skipped, no second request"


def test_warm_clears_the_link_and_returns_how_many_names_are_known(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeLangfuse(result=_stored("any", "text"))
    monkeypatch.setattr(observability, "client", lambda: fake)

    loaded = prompts.warm({"catalog-match-system": "a", "vision-instructions": "b {{subject}}"})

    assert loaded == 2
    assert prompts.take_linked_prompt() is None, "warming must not leave a link for the next real call"


# --- F3: placeholder-drift guard ------------------------------------------


def test_a_placeholder_mismatch_uses_the_code_text_and_warns_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    default = "Identify {{subject}} in {{country_list}}."
    stored = _stored("vision-instructions", "Identify {{subject}}.", version=5)  # missing {{country_list}}
    fake = FakeLangfuse(result=stored)
    monkeypatch.setattr(observability, "client", lambda: fake)
    prompts.warm({"vision-instructions": default})

    with caplog.at_level(logging.WARNING, logger="marketmind-scoring.prompts"):
        text = prompts.prompt_text("vision-instructions", default, subject="this product", country_list="BRA")
        text_again = prompts.prompt_text("vision-instructions", default, subject="that product", country_list="USA")

    assert text == "Identify this product in BRA."
    assert text_again == "Identify that product in USA."
    assert prompts.take_linked_prompt() is None

    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "vision-instructions" in warnings[0].getMessage()
    assert "v5" in warnings[0].getMessage()

    fake.requests.clear()
    prompts.prompt_text("vision-instructions", default, subject="x", country_list="y")
    assert fake.requests, "the name stays known so a corrected version is picked up on the next refresh"


def test_the_label_comes_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """So an experiment can score a candidate version without touching runtime,
    where the label stays production."""
    from app.core.config import settings

    stored = _stored("enrichment-system", "candidate text")
    fake = FakeLangfuse(result=stored)
    monkeypatch.setattr(observability, "client", lambda: fake)
    monkeypatch.setattr(settings, "langfuse_prompt_label", "candidate")
    prompts._known.add("enrichment-system")

    prompts.prompt_text("enrichment-system", "code text")

    assert fake.requests[0][1]["label"] == "candidate"
