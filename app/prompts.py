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
"""

import logging
from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any

from langfuse.model import TemplateParser

from app import observability

logger = logging.getLogger("marketmind-scoring.prompts")

LABEL = "production"
_FETCH_TIMEOUT_SECONDS = 3

_linked: ContextVar[Any | None] = ContextVar("linked_prompt", default=None)


def prompt_text(name: str, default: str, **variables: str) -> str:
    """The instruction called `name`, with its variables filled in."""
    client = observability.client()
    if client is None:
        return TemplateParser.compile_template(default, variables)
    try:
        stored = client.get_prompt(
            name,
            label=LABEL,
            type="text",
            fallback=default,
            max_retries=1,
            fetch_timeout_seconds=_FETCH_TIMEOUT_SECONDS,
        )
        text = stored.compile(**variables)
    except Exception:  # noqa: BLE001 — a prompt that cannot be fetched is the text in code
        logger.debug("Could not fetch prompt %s; using the text in code", name, exc_info=True)
        _linked.set(None)
        return TemplateParser.compile_template(default, variables)
    # A fallback is the code text wearing a prompt's clothes: Langfuse cannot
    # link a generation to it, and it must not inherit a stale link either.
    _linked.set(None if stored.is_fallback else stored)
    return text


def take_linked_prompt() -> Any | None:
    """The prompt fetched for the next generation, handed over once."""
    stored = _linked.get()
    _linked.set(None)
    return stored


def warm(registry: Mapping[str, str]) -> None:
    """Fetch every prompt once, so no request pays the first fetch.

    get_prompt is a synchronous HTTP call; inside a request it would block the
    event loop for as long as Langfuse takes to answer. Run from a thread at
    startup, the fetches land in the SDK's cache, which then refreshes itself
    in the background.
    """
    for name, default in registry.items():
        prompt_text(name, default)
    take_linked_prompt()
    logger.info("Warmed %d prompts from Langfuse", len(registry))
