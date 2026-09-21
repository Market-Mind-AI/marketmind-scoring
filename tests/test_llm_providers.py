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
