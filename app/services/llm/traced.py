"""One Langfuse generation per model call, whatever the provider.

A wrapper rather than a change in each provider: the providers are pure
transport (see base.py), and tracing belongs to the call site — which prompt,
for which step — not to who answered. The site name becomes the generation's
name, so a trace reads "enrichment", "explanation", not "ClaudeProvider".

Tracing never fails a call. Every Langfuse operation is guarded; the provider's
answer, or its exception, passes through unchanged.
"""

import logging
from typing import Any

from pydantic import BaseModel

from app import observability, prompts
from app.services.llm import usage
from app.services.llm.base import ImageInput, LLMProvider, joined_system

logger = logging.getLogger("marketmind-scoring.llm_tracing")


def describe_image(image: ImageInput) -> str:
    """What the trace shows instead of the photo. The photo stays here."""
    size = len(image.base64_data) * 3 // 4
    readable = f"{size / 1_000_000:.1f} MB" if size >= 1_000_000 else f"{round(size / 1000)} KB"
    return f"[{image.media_type}, {readable}]"


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
        generation = self._start(joined_system(system, system_tail), user, image)
        before = _tokens(usage.current())
        # The tail only when there is one, so a provider written before the
        # split (the test doubles) keeps its signature.
        tail = {"system_tail": system_tail} if system_tail else {}
        try:
            result = await self.inner.complete(
                system=system, user=user, image=image, response_model=response_model, **tail
            )
        except BaseException as exc:
            _finish(generation, level="ERROR", status_message=str(exc), usage_details=_spent_since(before))
            raise
        _finish(generation, output=result, usage_details=_spent_since(before))
        return result

    def _start(self, system: str | None, user: str, image: ImageInput | None) -> Any | None:
        client = observability.client()
        if client is None:
            return None
        try:
            recorded: dict[str, Any] = {"system": system, "user": user} if system else {"user": user}
            if image is not None:
                recorded["image"] = describe_image(image)
            linked = prompts.take_linked_prompt()
            return client.start_observation(
                name=self.site,
                as_type="generation",
                model=self.model,
                input=recorded,
                metadata={"provider": type(self.inner).__name__},
                **({"prompt": linked} if linked is not None else {}),
            )
        except Exception:  # noqa: BLE001 — tracing never fails a call
            logger.debug("Could not open the %s generation", self.site, exc_info=True)
            return None


def _as_output(result: str | BaseModel) -> Any:
    return result.model_dump(mode="json") if isinstance(result, BaseModel) else result


def _tokens(tally: usage.Usage | None) -> tuple[int, int, int, int, int] | None:
    if tally is None:
        return None
    return (
        tally.input_tokens,
        tally.output_tokens,
        tally.calls_without_usage,
        tally.cache_read_tokens,
        tally.cache_write_tokens,
    )


def _spent_since(before: tuple[int, int, int, int, int] | None) -> dict[str, int] | None:
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
    # Langfuse prices each usage type on its own, so `input` is the uncached
    # remainder here and the cached parts go under their own keys — reporting
    # the whole prompt as `input` would price cache reads at full rate.
    spent = {"input": after[0] - before[0] - lido - escrito, "output": after[1] - before[1]}
    if lido:
        spent["input_cache_read"] = lido
    if escrito:
        spent["input_cache_creation"] = escrito
    return spent


def _finish(generation: Any | None, **update: Any) -> None:
    if generation is None:
        return
    try:
        if "output" in update:
            update["output"] = _as_output(update["output"])
        generation.update(**{k: v for k, v in update.items() if v is not None})
        generation.end()
    except Exception:  # noqa: BLE001 — tracing never fails a call
        logger.debug("Could not close a generation", exc_info=True)
