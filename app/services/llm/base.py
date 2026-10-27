"""Provider-agnostic LLM abstraction (2026-07-22). Every LLM call site in the
core goes through a provider resolved from the env (see resolver.py), so the
provider (claude / gpt / ollama) and model can be swapped by editing .env only —
no code change, and each site is independent.

A provider exposes ONE method, ``complete``, covering the three shapes the core
needs: plain text, vision (an image), and structured output (a Pydantic model).
The prompt is always built by the caller (the core) and passed in — providers
are pure transport, so switching providers never changes what is asked, only who
answers. Consistent with the project principle that the LLM explains/identifies
but never computes MOS values (Cap. 4 §4.0)."""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pydantic import BaseModel

from app.core.exceptions import MarketMindCoreError


class LLMError(MarketMindCoreError):
    """An LLM provider call failed or returned an unusable result."""


@dataclass(frozen=True)
class ImageInput:
    """A base64-encoded image for a vision call."""

    base64_data: str
    media_type: str = "image/jpeg"


# Recebe o texto acumulado até agora, a cada pedaço que o modelo escreve.
TextSink = Callable[[str], Awaitable[None]]


class LLMProvider(ABC):
    """One resolved (provider, model) pair. ``complete`` returns a plain string
    when ``response_model`` is None, or a validated instance of it otherwise
    (callers pass a response_model and narrow the result with isinstance).

    ``system`` is the part of the instructions that is the same from one call
    to the next; ``system_tail`` is the part that changes per request (a brand,
    the user's own words). They are read as one system prompt, tail last. The
    split exists for prompt caching: a provider that caches a prefix (Claude)
    caches ``system`` and sends the tail after it, so a per-request detail does
    not turn every call into a fresh cache write. Providers without that just
    join the two.

    ``on_text``, for a plain-text call, receives the text written so far while
    the model writes it, so a long answer can be shown before it ends. A
    provider that cannot stream ignores it and only returns the whole text."""

    def __init__(self, model: str) -> None:
        self.model = model
        # The call site this provider serves ("vision", "explanation", ...),
        # set by the resolver. Only for the usage log line; never part of the
        # request.
        self.site: str | None = None

    @abstractmethod
    async def complete(
        self,
        *,
        system: str | None = None,
        system_tail: str | None = None,
        user: str,
        image: ImageInput | None = None,
        response_model: type[BaseModel] | None = None,
        on_text: TextSink | None = None,
    ) -> str | BaseModel: ...


def joined_system(system: str | None, system_tail: str | None) -> str | None:
    """``system`` and ``system_tail`` as the single prompt they stand for."""
    parts = [part for part in (system, system_tail) if part]
    return "\n\n".join(parts) if parts else None
