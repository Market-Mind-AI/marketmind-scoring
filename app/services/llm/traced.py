"""One Langfuse generation per model call, whatever the provider.

A wrapper rather than a change in each provider: the providers are pure
transport (see base.py), and tracing belongs to the call site — which prompt,
for which step — not to who answered. The site name becomes the generation's
name, so a trace reads "enrichment", "explanation", not "ClaudeProvider".

Every generation carries the same set of facts, so the Langfuse views that
filter on them (level, latency, cost, tokens) see every call the same way:

- ``level`` / ``status_message``: ERROR when the provider raised, with the
  exception; WARNING when the call ran but degraded — the provider reported no
  token usage (the call cannot be priced), or the instructions came from the
  code text instead of the prompt version Langfuse holds (the generation cannot
  be linked to a prompt). DEFAULT otherwise. A degraded call that stayed
  DEFAULT would be invisible exactly where someone looks for it.
- ``model`` and ``usage_details``, split the way the provider bills them, so
  Langfuse prices the call from its own model table.
- latency, which the span measures itself.

Tracing never fails a call. Every Langfuse operation is guarded; the provider's
answer, or its exception, passes through unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from app import observability, pricing, prompts
from app.services.llm import usage
from app.services.llm.base import ImageInput, LLMProvider, joined_system

logger = logging.getLogger("marketmind-scoring.llm_tracing")

NO_USAGE_WARNING = "the provider reported no token usage; the call is not priced"


def describe_image(image: ImageInput) -> str:
    """What the trace shows instead of the photo. The photo stays here."""
    size = len(image.base64_data) * 3 // 4
    readable = f"{size / 1_000_000:.1f} MB" if size >= 1_000_000 else f"{round(size / 1000)} KB"
    return f"[{image.media_type}, {readable}]"


@dataclass
class _Open:
    """A generation that has been opened, and what is already known to be
    wrong with the call it records."""

    generation: Any
    warnings: list[str] = field(default_factory=list)


class TracedProvider(LLMProvider):
    def __init__(self, inner: LLMProvider, site: str) -> None:
        super().__init__(inner.model)
        self.inner = inner
        self.site = site

    async def complete(
        self,
        *,
        system: str | None = None,
        system_tail: str | None = None,
        user: str,
        image: ImageInput | None = None,
        response_model: type[BaseModel] | None = None,
    ) -> str | BaseModel:
        opened = self._start(joined_system(system, system_tail), user, image)
        before = _tokens(usage.current())
        # The tail only when there is one, so a provider written before the
        # split (the test doubles) keeps its signature.
        tail = {"system_tail": system_tail} if system_tail else {}
        try:
            result = await self.inner.complete(
                system=system, user=user, image=image, response_model=response_model, **tail
            )
        except BaseException as exc:
            spent = _spent_since(before)
            _charge(self.model, spent, before)
            _finish(
                opened,
                level="ERROR",
                status_message=f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__,
                usage_details=spent,
            )
            raise
        spent = _spent_since(before)
        _charge(self.model, spent, before)
        if spent is None and _reported_no_usage(before):
            _warn(opened, NO_USAGE_WARNING)
        _finish(opened, output=result, usage_details=spent)
        return result

    def _start(self, system: str | None, user: str, image: ImageInput | None) -> _Open | None:
        client = observability.client()
        if client is None:
            return None
        try:
            recorded: dict[str, Any] = {"system": system, "user": user} if system else {"user": user}
            if image is not None:
                recorded["image"] = describe_image(image)
            linked = prompts.take_linked_prompt()
            fallback = prompts.take_fallback_reason()
            opened = _Open(
                client.start_observation(
                    name=self.site,
                    as_type="generation",
                    model=self.model,
                    input=recorded,
                    metadata={"provider": type(self.inner).__name__, "site": self.site},
                    **({"prompt": linked} if linked is not None else {}),
                )
            )
            if fallback:
                _warn(opened, fallback)
            return opened
        except Exception:  # noqa: BLE001 — tracing never fails a call
            logger.debug("Could not open the %s generation", self.site, exc_info=True)
            return None


def _charge(model: str, spent: dict[str, int] | None, before: _Tally | None) -> None:
    """Price the call that just ran into the request's tally. Only when a
    tally is open and the call was counted in it: outside a request there is
    nothing to add to, and a call the provider never reported stays unpriced
    (counted as such by `charge`), never free."""
    if before is None or usage.current() is None:
        return
    usage.charge(model, pricing.price(model, spent))


def _warn(opened: _Open | None, message: str) -> None:
    if opened is not None:
        opened.warnings.append(message)


def _as_output(result: str | BaseModel) -> Any:
    return result.model_dump(mode="json") if isinstance(result, BaseModel) else result


# (input, output, calls_without_usage, cache_read, cache_write, cache_write_5m, cache_write_1h)
_Tally = tuple[int, int, int, int, int, int, int]


def _tokens(tally: usage.Usage | None) -> _Tally | None:
    if tally is None:
        return None
    return (
        tally.input_tokens,
        tally.output_tokens,
        tally.calls_without_usage,
        tally.cache_read_tokens,
        tally.cache_write_tokens,
        tally.cache_write_5m_tokens,
        tally.cache_write_1h_tokens,
    )


def _reported_no_usage(before: _Tally | None) -> bool:
    """Whether the call that just ran added to ``calls_without_usage``."""
    after = _tokens(usage.current())
    return before is not None and after is not None and after[2] > before[2]


def _spent_since(before: _Tally | None) -> dict[str, int] | None:
    """The token delta since ``before``, or None when there is nothing to report.

    That is either because no tally is open, or because the call that just ran
    added to ``calls_without_usage`` — the provider reported no usage figures at
    all, which is not the same as spending zero tokens and must not be recorded
    as a free call.
    """
    after = _tokens(usage.current())
    if before is None or after is None or after[2] > before[2]:
        return None
    lido, escrito = after[3] - before[3], after[4] - before[4]
    cinco_min, uma_hora = after[5] - before[5], after[6] - before[6]
    # Langfuse prices each usage type on its own, so `input` is the uncached
    # remainder here and the cached parts go under their own keys — reporting
    # the whole prompt as `input` would price cache reads at full rate.
    spent = {"input": after[0] - before[0] - lido - escrito, "output": after[1] - before[1]}
    if lido:
        spent["input_cache_read"] = lido
    if escrito:
        # By TTL when the provider says, since a 1-hour write costs 1.6x a
        # 5-minute one; the undifferentiated key only for the remainder.
        if cinco_min:
            spent["input_cache_creation_5m"] = cinco_min
        if uma_hora:
            spent["input_cache_creation_1h"] = uma_hora
        resto = escrito - cinco_min - uma_hora
        if resto > 0:
            spent["input_cache_creation"] = resto
    return spent


def _finish(opened: _Open | None, **update: Any) -> None:
    if opened is None:
        return
    try:
        if "output" in update:
            update["output"] = _as_output(update["output"])
        if opened.warnings and update.get("level") != "ERROR":
            update["level"] = "WARNING"
            update["status_message"] = "; ".join(opened.warnings)
        opened.generation.update(**{k: v for k, v in update.items() if v is not None})
        opened.generation.end()
    except Exception:  # noqa: BLE001 — tracing never fails a call
        logger.debug("Could not close a generation", exc_info=True)
