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

Langfuse never caches a fallback, and its HTTP client retries 5xx/timeouts on
its own — so a name Langfuse has never confirmed would pay a synchronous
0.4-7 s fetch on the event loop on every single call, cold or down. `_known`
is the set of names Langfuse has confirmed it holds; only those are ever
fetched inline (a cache hit, or a stale hit the SDK refreshes in the
background). Everything else uses the code text with no network on the
request path. `warm` (and the keep-warm loop in app/main.py) is what moves a
name into `_known`.
"""

import logging
from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any

from langfuse.model import TemplateParser

from app import observability
from app.core.config import settings

logger = logging.getLogger("marketmind-scoring.prompts")

LABEL = "production"
_FETCH_TIMEOUT_SECONDS = 3

_linked: ContextVar[Any | None] = ContextVar("linked_prompt", default=None)

# Why the last prompt_text call answered with the code text although Langfuse
# is configured — None when it answered with the stored prompt, or when there
# is no Langfuse to answer from. The generation that follows reports it as a
# WARNING: a model that ran on the code text cannot be linked to a prompt
# version, and a trace that keeps quiet about it reads like a healthy one.
_fallback_reason: ContextVar[str | None] = ContextVar("prompt_fallback_reason", default=None)

# Names Langfuse has confirmed it holds. Only these are ever fetched inline;
# everything else uses the code text with no network on the request path.
_known: set[str] = set()

# (name, version) pairs already warned about a placeholder mismatch (F3),
# so a persistent drift logs once rather than on every call.
_warned_drift: set[tuple[str, int]] = set()

# The loaded count warm() last logged a warning for, so the keep-warm loop
# (every 60 s) doesn't repeat the same warning forever. None before the
# first call.
_last_reported: int | None = None


def prompt_text(name: str, default: str, **variables: str) -> str:
    """The instruction called `name`, with its variables filled in."""
    compiled_default = TemplateParser.compile_template(default, variables)
    client = observability.client()
    _fallback_reason.set(None)
    if client is None:
        # Unconfigured: no network on the request path, and nothing to warn.
        _linked.set(None)
        return compiled_default
    if name not in _known:
        # A name Langfuse has never confirmed: no network on the request path.
        _linked.set(None)
        _fallback_reason.set(f"prompt {name} came from the code text (not confirmed by Langfuse yet)")
        return compiled_default
    try:
        stored = client.get_prompt(
            name,
            label=settings.langfuse_prompt_label,
            type="text",
            fallback=default,
            max_retries=0,
            fetch_timeout_seconds=_FETCH_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 — a prompt that cannot be fetched is the text in code
        logger.debug("Could not fetch prompt %s; using the text in code", name, exc_info=True)
        _known.discard(name)
        _linked.set(None)
        _fallback_reason.set(f"prompt {name} came from the code text (the fetch failed)")
        return compiled_default
    # A fallback is the code text wearing a prompt's clothes: Langfuse cannot
    # link a generation to it, and it must not inherit a stale link either.
    # It also means Langfuse no longer confirms this name, so the next call
    # goes back to the code text with no network, until warm() reconfirms it.
    if stored.is_fallback:
        _known.discard(name)
        _linked.set(None)
        _fallback_reason.set(f"prompt {name} came from the code text (Langfuse answered with the fallback)")
        return compiled_default
    expected = TemplateParser.find_variable_names(default)
    if set(stored.variables) != set(expected):
        _warn_about_drift(name, stored, expected)
        _linked.set(None)
        _fallback_reason.set(
            f"prompt {name} came from the code text (v{stored.version} in Langfuse has different variables)"
        )
        return compiled_default
    _linked.set(stored)
    return stored.compile(**variables)


def _warn_about_drift(name: str, stored: Any, expected: list[str]) -> None:
    """A stored prompt whose variables don't match the code default (F3):
    using it would silently leave a placeholder unfilled, or drop one the
    code text relies on. Logged once per (name, version) — the name stays in
    `_known`, so a corrected version is picked up on the next refresh."""
    key = (name, stored.version)
    if key in _warned_drift:
        return
    _warned_drift.add(key)
    logger.warning(
        "Prompt %s v%s in Langfuse has variables %s, expected %s; using the text in code",
        name,
        stored.version,
        stored.variables,
        expected,
    )


def take_linked_prompt() -> Any | None:
    """The prompt fetched for the next generation, handed over once."""
    stored = _linked.get()
    _linked.set(None)
    return stored


def take_fallback_reason() -> str | None:
    """Why the next generation runs on the code text, handed over once; None
    when it runs on the stored prompt or when Langfuse is not configured."""
    reason = _fallback_reason.get()
    _fallback_reason.set(None)
    return reason


def warm(registry: Mapping[str, str]) -> int:
    """Confirm every not-yet-known name once, so a request never pays the
    first fetch.

    get_prompt is a synchronous HTTP call; inside a request it would block the
    event loop for as long as Langfuse takes to answer. Run from a thread,
    repeatedly (see app.main's keep-warm loop), so a name seeded or recovered
    after startup still gets off the request path on its own. A name already
    in `_known` is skipped: steady state makes no request at all.

    Returns how many of the registry's names are now in `_known`.
    """
    client = observability.client()
    if client is None:
        return 0
    for name in registry:
        if name in _known:
            continue
        try:
            stored = client.get_prompt(
                name,
                label=settings.langfuse_prompt_label,
                type="text",
                max_retries=0,
                fetch_timeout_seconds=_FETCH_TIMEOUT_SECONDS,
            )
        except Exception:  # noqa: BLE001 — an unconfirmed name just stays unconfirmed
            logger.debug("Could not warm prompt %s; using the text in code", name, exc_info=True)
            continue
        if not stored.is_fallback:
            _known.add(name)
    take_linked_prompt()
    loaded = sum(1 for name in registry if name in _known)
    _report(loaded, len(registry))
    return loaded


def _report(loaded: int, total: int) -> None:
    global _last_reported
    if loaded == total:
        logger.info("Warmed %d of %d prompts from Langfuse", loaded, total)
    elif loaded != _last_reported:
        logger.warning("Warmed %d of %d prompts from Langfuse; using the code text for the rest", loaded, total)
    _last_reported = loaded
