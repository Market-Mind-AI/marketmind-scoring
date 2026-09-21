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
