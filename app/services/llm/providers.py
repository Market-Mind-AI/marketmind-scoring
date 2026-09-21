"""The three LLM providers behind the resolver: Claude (Anthropic SDK), GPT
(OpenAI SDK), and Ollama (routed through the marketmind-ollma-llm gateway).
Each implements the same ``complete`` contract (see base.py)."""

from typing import Any

import httpx
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from pydantic import BaseModel

from app.services.llm.base import ImageInput, LLMError, LLMProvider


class ClaudeProvider(LLMProvider):
    def __init__(self, model: str, api_key: str, workspace_id: str | None = None) -> None:
        super().__init__(model)
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
        if system:
            kwargs["system"] = system

        if response_model is not None:
            response = await self._client.messages.parse(output_format=response_model, **kwargs)
            if response.parsed_output is None:
                raise LLMError(f"Claude returned no parseable result (stop_reason={response.stop_reason})")
            return response.parsed_output
        response = await self._client.messages.create(**kwargs)
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
        user: str,
        image: ImageInput | None = None,
        response_model: type[BaseModel] | None = None,
    ) -> str | BaseModel:
        user_content: list[dict[str, Any]] = [{"type": "text", "text": user}]
        if image is not None:
            data_uri = f"data:{image.media_type};base64,{image.base64_data}"
            user_content.append({"type": "image_url", "image_url": {"url": data_uri}})
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user_content})

        if response_model is not None:
            parsed = await self._client.beta.chat.completions.parse(
                model=self.model,
                messages=messages,  # type: ignore[arg-type]
                response_format=response_model,
            )
            result = parsed.choices[0].message.parsed
            if result is None:
                raise LLMError("GPT returned no parseable structured result")
            return result
        completion = await self._client.chat.completions.create(
            model=self.model,
            messages=messages,  # type: ignore[arg-type]
        )
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
        user: str,
        image: ImageInput | None = None,
        response_model: type[BaseModel] | None = None,
    ) -> str | BaseModel:
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
            content = response.json().get("content")
        if not content:
            raise LLMError("Ollama gateway returned empty content")
        if response_model is not None:
            return response_model.model_validate_json(content)
        return str(content)
