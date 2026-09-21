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
