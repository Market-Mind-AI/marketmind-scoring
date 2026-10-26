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


def test_the_registry_holds_the_eight_instructions_from_code() -> None:
    assert PROMPTS == {
        "catalog-match-system": catalog_matcher._SYSTEM,
        "catalog-edit-review-system": catalog_matcher._EDIT_REVIEW_SYSTEM,
        "enrichment-system": enrichment_client._SYSTEM,
        "reference-market-system": reference_market_client._SYSTEM,
        "vision-system": vision_client._INSTRUCTIONS,
        "explanation-system-pt": explanation_client._SYSTEM_PROMPT_PT,
        "explanation-system-en": explanation_client._SYSTEM_PROMPT_EN,
        "explanation-system-es": explanation_client._SYSTEM_PROMPT_ES,
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
