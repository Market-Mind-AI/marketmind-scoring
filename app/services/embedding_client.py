"""Embedding provider for the product catalog's semantic-match step
(catalog_service pipeline step 3). OpenAI is a deliberate stopgap — Anthropic
has no first-party embeddings endpoint (their own docs point to Voyage AI) —
chosen 2026-07-02 ahead of a planned move to a locally-hosted model. Isolated
behind this one class specifically so that swap is a single-file change: no
other module should import ``httpx``/OpenAI specifics for embeddings.
"""

import httpx

from app.core.config import settings
from app.core.exceptions import MarketMindCoreError

_OPENAI_EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"


class EmbeddingError(MarketMindCoreError):
    """The embedding provider call failed or returned something we can't use."""


class EmbeddingClient:
    async def embed(self, text: str) -> list[float]:
        if not settings.openai_api_key:
            raise EmbeddingError("OPENAI_API_KEY is not configured")

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    _OPENAI_EMBEDDINGS_URL,
                    headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                    json={
                        "input": text,
                        "model": settings.openai_embedding_model,
                        "dimensions": settings.openai_embedding_dimensions,
                    },
                )
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"Failed to call OpenAI embeddings API: {exc}") from exc

        try:
            embedding = body["data"][0]["embedding"]
        except (KeyError, IndexError) as exc:
            raise EmbeddingError(f"Unexpected OpenAI embeddings response shape: {body}") from exc

        if len(embedding) != settings.openai_embedding_dimensions:
            raise EmbeddingError(f"Expected {settings.openai_embedding_dimensions}-dim embedding, got {len(embedding)}")
        return list(embedding)
