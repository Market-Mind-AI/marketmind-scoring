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


def test_langfuse_constructor_failure_does_not_fail_the_service() -> None:
    with (
        patch.object(settings, "langfuse_public_key", "pk-test-core"),
        patch.object(settings, "langfuse_secret_key", "sk-test"),
        patch("langfuse.Langfuse", side_effect=ValueError("bad sample rate")),
    ):
        assert observability.client() is None
        assert observability.tracing_enabled() is False


def test_adopting_a_callers_traceparent_makes_it_the_current_trace() -> None:
    from opentelemetry import trace

    trace_id = "99eadfa97c641404b5c53a5958a11f14"
    headers = {"traceparent": f"00-{trace_id}-b88f74cfd738d647-01"}

    with observability.adopt_trace_context(headers):
        inside = trace.get_current_span().get_span_context()
    outside = trace.get_current_span().get_span_context()

    assert format(inside.trace_id, "032x") == trace_id
    assert not outside.is_valid, "the caller's context must not leak past the request"
