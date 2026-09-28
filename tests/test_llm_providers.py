"""How the Claude provider is constructed — the part that decides whether a
request is even accepted."""

import pytest

from app.services.llm.base import LLMError
from app.services.llm.providers import ClaudeProvider


def _headers(provider: ClaudeProvider) -> dict:
    return dict(provider._client._custom_headers or {})


def test_a_workspace_scoped_key_sends_no_workspace_header() -> None:
    """It carries the workspace in the key itself. An empty header would be
    worse than none — Anthropic reads it as naming no workspace."""
    provider = ClaudeProvider("claude-opus-5", "sk-ant-test", workspace_id="")

    assert "anthropic-workspace-id" not in _headers(provider)


def test_an_organisation_key_says_which_workspace_to_use() -> None:
    """Anthropic refuses an organisation-scoped key without it: "This API key is
    not scoped to a workspace, so this request must include the
    anthropic-workspace-id header"."""
    provider = ClaudeProvider("claude-opus-5", "sk-ant-test", workspace_id="wrkspc_123")

    assert _headers(provider)["anthropic-workspace-id"] == "wrkspc_123"


def test_no_key_at_all_fails_where_it_can_be_read() -> None:
    """At construction, naming the variable — not deep inside a call, where it
    surfaces as a 401 on whatever the analysis happened to be doing."""
    with pytest.raises(LLMError) as excinfo:
        ClaudeProvider("claude-opus-5", "")

    assert "ANTHROPIC_API_KEY" in str(excinfo.value)


class _Messages:
    def __init__(self) -> None:
        self.sent: dict = {}

    async def create(self, **kwargs):  # noqa: ANN003, ANN201
        from types import SimpleNamespace

        self.sent = kwargs
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")], usage=None)


async def test_the_stable_instructions_are_cached_and_the_per_request_tail_is_not() -> None:
    """Every call here ends in unique content, so the breakpoint belongs at the
    end of what repeats. Past it, a brand or the user's words would make each
    call write a cache entry that no later call reads."""
    provider = ClaudeProvider("claude-opus-5", "sk-ant-test")
    provider._client.messages = _Messages()  # type: ignore[assignment]

    await provider.complete(system="Stable rules.", system_tail="Brand: Cardhu.", user="Explain")

    assert provider._client.messages.sent["system"] == [
        {"type": "text", "text": "Stable rules.", "cache_control": {"type": "ephemeral", "ttl": "1h"}},
        {"type": "text", "text": "Brand: Cardhu."},
    ]
    assert "cache_control" not in provider._client.messages.sent


async def test_no_instructions_send_no_system() -> None:
    provider = ClaudeProvider("claude-opus-5", "sk-ant-test")
    provider._client.messages = _Messages()  # type: ignore[assignment]

    await provider.complete(user="What is this?")

    assert "system" not in provider._client.messages.sent


async def test_the_cache_lifetime_is_the_configured_one() -> None:
    provider = ClaudeProvider("claude-opus-5", "sk-ant-test", cache_ttl="5m")
    provider._client.messages = _Messages()  # type: ignore[assignment]

    await provider.complete(system="Stable rules.", user="Explain")

    assert provider._client.messages.sent["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
