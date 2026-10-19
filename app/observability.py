"""Tracing for the model calls this service makes.

The orchestrator decides; the model calls that decision causes happen here. So
its trace can say which step ran but not what a model was asked, what it
answered, or what that cost — unless this service reports them into the same
trace. The orchestrator sends the W3C `traceparent` of its run with every
request; adopting it puts each model call inside the run's trace, under its
`analysis` span — LangChain makes no node span current in node code, so a
generation nests under the run, not under the individual step or node.

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

logger = logging.getLogger("marketmind-scoring.observability")


@lru_cache(maxsize=1)
def client() -> Any | None:
    """The Langfuse client, or None when tracing is not configured."""
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        return None

    try:
        from langfuse import Langfuse

        return Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
    except Exception:  # noqa: BLE001 — tracing setup never fails a call
        logger.warning("Langfuse could not be configured; model calls will not be traced.", exc_info=True)
        return None


def tracing_enabled() -> bool:
    return client() is not None


class Observation:
    """A handle on one open observation. Every method is safe to call when
    tracing is off or broken: it then does nothing."""

    def __init__(self, span: Any | None) -> None:
        self._span = span

    def update(self, **fields: Any) -> None:
        if self._span is None:
            return
        try:
            self._span.update(**{k: v for k, v in fields.items() if v is not None})
        except Exception:  # noqa: BLE001 — tracing never fails the work it records
            logger.debug("Could not update an observation", exc_info=True)

    def record(self, name: str, *, as_type: str = "generation", **fields: Any) -> None:
        """One instantaneous child observation: a fact that has its own cost
        or usage, which the SDK only records on generation-like types. That
        is how a collector's DataForSEO bill lands in the trace priced, under
        the dispatch that spent it, without calling the dispatch a model."""
        if self._span is None:
            return
        try:
            child = self._span.start_observation(
                name=name, as_type=as_type, **{k: v for k, v in fields.items() if v is not None}
            )
            child.end()
        except Exception:  # noqa: BLE001
            logger.debug("Could not record the observation %s", name, exc_info=True)


@contextmanager
def observe(name: str, *, as_type: str = "tool", **fields: Any) -> Iterator[Observation]:
    """Record one unit of non-model work in the current trace — a call to a
    collector, say — with its latency, and ERROR with the exception if the
    body raises. The exception passes through unchanged.

    `fields` are the observation's opening attributes (input, metadata...).
    A no-op when tracing is not configured, and a tracing failure never
    reaches the body: a measurement that breaks the thing it measures is
    worse than no measurement.
    """
    span = None
    langfuse = client()
    if langfuse is not None:
        try:
            span = langfuse.start_observation(name=name, as_type=as_type, **fields)
        except Exception:  # noqa: BLE001
            logger.debug("Could not open the observation %s", name, exc_info=True)
    handle = Observation(span)
    try:
        yield handle
    except BaseException as exc:
        handle.update(level="ERROR", status_message=f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__)
        _end(span)
        raise
    _end(span)


def _end(span: Any | None) -> None:
    if span is None:
        return
    try:
        span.end()
    except Exception:  # noqa: BLE001
        logger.debug("Could not end an observation", exc_info=True)


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
