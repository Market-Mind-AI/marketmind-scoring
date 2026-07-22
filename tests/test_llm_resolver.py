from unittest.mock import patch

import pytest

from app.core.config import settings
from app.services.llm import LLMError, resolve_llm
from app.services.llm.providers import ClaudeProvider, GptProvider, OllamaProvider


def test_ollama_provider_needs_no_key() -> None:
    provider = resolve_llm(provider="ollama", model="gemma4:12b")
    assert isinstance(provider, OllamaProvider)
    assert provider.model == "gemma4:12b"


def test_claude_provider_selected_with_key() -> None:
    with patch.object(settings, "anthropic_api_key", "test-key"):
        provider = resolve_llm(provider="claude", model="claude-opus-4-8")
    assert isinstance(provider, ClaudeProvider)
    assert provider.model == "claude-opus-4-8"


def test_gpt_provider_selected_with_key() -> None:
    with patch.object(settings, "openai_api_key", "test-key"):
        provider = resolve_llm(provider="gpt", model="gpt-4o")
    assert isinstance(provider, GptProvider)


def test_provider_code_is_case_insensitive() -> None:
    assert isinstance(resolve_llm(provider="OLLAMA", model="x"), OllamaProvider)


def test_unknown_provider_raises() -> None:
    with pytest.raises(LLMError, match="Unknown LLM provider"):
        resolve_llm(provider="gemini", model="x")


def test_selected_provider_without_key_raises() -> None:
    with patch.object(settings, "anthropic_api_key", ""), pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        resolve_llm(provider="claude", model="claude-opus-4-8")
    with patch.object(settings, "openai_api_key", ""), pytest.raises(LLMError, match="OPENAI_API_KEY"):
        resolve_llm(provider="gpt", model="gpt-4o")
