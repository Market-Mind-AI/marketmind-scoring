"""The three LLM providers behind the resolver: Claude (Anthropic SDK), GPT
(OpenAI SDK), and Ollama (routed through the marketmind-ollma-llm gateway).
Each implements the same ``complete`` contract (see base.py)."""

import logging
from typing import Any

import httpx
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from pydantic import BaseModel

from app.services.llm import usage
from app.services.llm.base import ImageInput, LLMError, LLMProvider, joined_system

logger = logging.getLogger("marketmind-scoring.llm_usage")


def log_claude_usage(model: str, site: str | None, raw_usage: Any) -> None:
    """One line per Claude call with the billed token counts, exactly as the
    API reports them. The request's tally (usage.py) sums a whole HTTP request
    and folds the cache into the input; this is the per-call record that a
    cost has to be computed from — cache reads, writes per TTL and plain input
    are priced differently."""
    if raw_usage is None:
        logger.info(f"CLAUDE_USAGE site={site} model={model} usage=none")
        return
    creation = getattr(raw_usage, "cache_creation", None)
    logger.info(
        f"CLAUDE_USAGE site={site} model={model} input={raw_usage.input_tokens} "
        f"cache_read={raw_usage.cache_read_input_tokens or 0} "
        f"cache_write_5m={getattr(creation, 'ephemeral_5m_input_tokens', 0) or 0} "
        f"cache_write_1h={getattr(creation, 'ephemeral_1h_input_tokens', 0) or 0} "
        f"output={raw_usage.output_tokens}"
    )


class ClaudeProvider(LLMProvider):
    def __init__(self, model: str, api_key: str, workspace_id: str | None = None, cache_ttl: str = "1h") -> None:
        super().__init__(model)
        # See settings.llm_prompt_cache_ttl for why an hour is the default.
        self._cache_control = {"type": "ephemeral", "ttl": cache_ttl}
        if not api_key:
            raise LLMError("ANTHROPIC_API_KEY is not configured (required for the 'claude' LLM provider)")
        # An organisation-scoped key has to say which workspace to bill and
        # scope to; a workspace-scoped key carries that in the key and needs
        # nothing. Sending an empty header would be worse than sending none, so
        # it only goes on when there is a value.
        headers = {"anthropic-workspace-id": workspace_id} if workspace_id else None
        self._client = AsyncAnthropic(api_key=api_key, default_headers=headers)

    async def complete(
        self,
        *,
        system: str | None = None,
        system_tail: str | None = None,
        user: str,
        image: ImageInput | None = None,
        response_model: type[BaseModel] | None = None,
    ) -> str | BaseModel:
        content: list[dict[str, Any]] = []
        if image is not None:
            content.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": image.media_type, "data": image.base64_data},
                }
            )
        content.append({"type": "text", "text": user})
        # 16000, not the 8192 this carried until 2026-08-13: on Claude Opus 5
        # thinking is ON when the request omits the `thinking` field, and
        # max_tokens caps thinking PLUS the answer. 8192 was sized around the
        # answer alone, so the identify-image `markets` payload — the largest
        # structured output here — could be cut off mid-JSON once thinking
        # started consuming the same budget. 16000 keeps a non-streaming call
        # comfortably inside the SDK's HTTP timeout; going higher would mean
        # switching this path to streaming.
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 16000,
            "messages": [{"role": "user", "content": content}],
        }
        # The stable instructions carry the cache breakpoint; the per-request
        # tail goes after it. Every call here is one-shot and ends in unique
        # content (the product, the evidence packet, a photo), so the automatic
        # top-level breakpoint would land on that tail and pay the write premium
        # on bytes never read back. A system shorter than the model's minimum
        # (512 tokens on Opus 5) simply isn't cached — no error, no charge.
        blocks: list[dict[str, Any]] = []
        if system:
            blocks.append({"type": "text", "text": system, "cache_control": self._cache_control})
        if system_tail:
            blocks.append({"type": "text", "text": system_tail})
        if blocks:
            kwargs["system"] = blocks

        if response_model is not None:
            response = await self._client.messages.parse(output_format=response_model, **kwargs)
            usage.record(self.model, getattr(response, "usage", None))
            log_claude_usage(self.model, self.site, getattr(response, "usage", None))
            if response.parsed_output is None:
                raise LLMError(f"Claude returned no parseable result (stop_reason={response.stop_reason})")
            return response.parsed_output
        response = await self._client.messages.create(**kwargs)
        usage.record(self.model, getattr(response, "usage", None))
        log_claude_usage(self.model, self.site, getattr(response, "usage", None))
        return "".join(block.text for block in response.content if block.type == "text")


class GptProvider(LLMProvider):
    def __init__(self, model: str, api_key: str) -> None:
        super().__init__(model)
        if not api_key:
            raise LLMError("OPENAI_API_KEY is not configured (required for the 'gpt' LLM provider)")
        self._client = AsyncOpenAI(api_key=api_key)

    async def complete(
        self,
        *,
        system: str | None = None,
        system_tail: str | None = None,
        user: str,
        image: ImageInput | None = None,
        response_model: type[BaseModel] | None = None,
    ) -> str | BaseModel:
        user_content: list[dict[str, Any]] = [{"type": "text", "text": user}]
        if image is not None:
            data_uri = f"data:{image.media_type};base64,{image.base64_data}"
            user_content.append({"type": "image_url", "image_url": {"url": data_uri}})
        messages: list[dict[str, Any]] = []
        instructions = joined_system(system, system_tail)
        if instructions:
            messages.append({"role": "system", "content": instructions})
        messages.append({"role": "user", "content": user_content})

        if response_model is not None:
            parsed = await self._client.beta.chat.completions.parse(
                model=self.model,
                messages=messages,  # type: ignore[arg-type]
                response_format=response_model,
            )
            usage.record(self.model, getattr(parsed, "usage", None))
            result = parsed.choices[0].message.parsed
            if result is None:
                raise LLMError("GPT returned no parseable structured result")
            return result
        completion = await self._client.chat.completions.create(
            model=self.model,
            messages=messages,  # type: ignore[arg-type]
        )
        usage.record(self.model, getattr(completion, "usage", None))
        return completion.choices[0].message.content or ""


class OllamaProvider(LLMProvider):
    """Routes to the marketmind-ollma-llm gateway (a local Ollama model). Text
    goes to /chat, images to /vision; structured output is requested by passing
    the response model's JSON schema as the gateway's ``format``."""

    def __init__(self, model: str, base_url: str, timeout: float) -> None:
        super().__init__(model)
        self._base_url = base_url
        self._timeout = timeout

    async def complete(
        self,
        *,
        system: str | None = None,
        system_tail: str | None = None,
        user: str,
        image: ImageInput | None = None,
        response_model: type[BaseModel] | None = None,
    ) -> str | BaseModel:
        system = joined_system(system, system_tail)
        fmt = response_model.model_json_schema() if response_model is not None else None
        if image is not None:
            path = "/vision"
            payload: dict[str, Any] = {
                "model": self.model,
                "system": system,
                "prompt": user,
                "image_base64": image.base64_data,
                "image_media_type": image.media_type,
                "format": fmt,
            }
        else:
            path = "/chat"
            payload = {"model": self.model, "system": system, "prompt": user, "format": fmt}

        async with httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout) as client:
            response = await client.post(path, json=payload)
            response.raise_for_status()
            body = response.json()
            usage.record(self.model, body.get("usage"))
            content = body.get("content")
        if not content:
            raise LLMError("Ollama gateway returned empty content")
        if response_model is not None:
            return response_model.model_validate_json(content)
        return str(content)
