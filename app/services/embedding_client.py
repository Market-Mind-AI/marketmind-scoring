"""Embeddings da Voyage AI para a busca semântica do catálogo (passo 3a).

httpx direto e não o SDK `voyageai`: a API é um POST com uma lista de textos, e
o SDK traria dependência nova para a imagem por uma chamada só. Peso de imagem
é critério aqui — a tentativa local com sentence-transformers levou a imagem de
1,67GB para mais de 20GB (13/08/2026).

`input_type` é a parte que importa para o recall. A busca é ASSIMÉTRICA: uma
consulta curta (`quadcopter`) contra termos curados do catálogo (`drone`,
`dron con cámara`). A Voyage prefixa cada lado com uma instrução própria de
recuperação quando sabe qual é qual; os dois tipos continuam no mesmo espaço,
então o coseno entre eles segue válido.
"""

from typing import Literal

import httpx
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.core.exceptions import MarketMindCoreError
from app.core.logging import logger

InputType = Literal["query", "document"]

# Limite da API por requisição é 1000 textos; 128 mantém cada chamada pequena
# o bastante para um 429 custar pouco a refazer.
_LOTE = 128


class EmbeddingError(MarketMindCoreError):
    """A Voyage não respondeu, ou respondeu algo que não é um vetor por texto."""


def _transitorio(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and (
        exc.response.status_code == 429 or exc.response.status_code >= 500
    )


class EmbeddingClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport
        self.model = settings.voyage_embedding_model
        self.dimensions = settings.voyage_embedding_dimensions
        # Tokens cobrados nesta instância — o backfill reporta o total.
        self.tokens = 0

    @property
    def enabled(self) -> bool:
        return bool(settings.voyage_api_key)

    async def embed(self, texts: list[str], *, input_type: InputType) -> list[list[float]]:
        if not self.enabled:
            raise EmbeddingError("VOYAGE_API_KEY não configurada")
        vetores: list[list[float]] = []
        async with httpx.AsyncClient(
            base_url=settings.voyage_api_base_url,
            timeout=settings.voyage_timeout_seconds,
            headers={"Authorization": f"Bearer {settings.voyage_api_key}"},
            transport=self._transport,
        ) as client:
            for inicio in range(0, len(texts), _LOTE):
                vetores.extend(await self._lote(client, texts[inicio : inicio + _LOTE], input_type))
        return vetores

    async def _lote(self, client: httpx.AsyncClient, texts: list[str], input_type: InputType) -> list[list[float]]:
        payload = {
            "input": texts,
            "model": self.model,
            "input_type": input_type,
            "output_dimension": self.dimensions,
        }
        try:
            async for tentativa in AsyncRetrying(
                retry=retry_if_exception(_transitorio),
                stop=stop_after_attempt(4),
                wait=wait_exponential(multiplier=1, max=20),
                reraise=True,
            ):
                with tentativa:
                    resposta = await client.post("/embeddings", json=payload)
                    resposta.raise_for_status()
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"Voyage falhou: {exc}") from exc

        corpo = resposta.json()
        dados = sorted(corpo.get("data") or [], key=lambda d: d["index"])
        if len(dados) != len(texts) or any(len(d["embedding"]) != self.dimensions for d in dados):
            raise EmbeddingError(
                f"Voyage devolveu {len(dados)} vetores para {len(texts)} textos, ou dimensão diferente de "
                f"{self.dimensions}"
            )
        tokens = int((corpo.get("usage") or {}).get("total_tokens") or 0)
        self.tokens += tokens
        logger.info(f"Voyage {self.model}: {len(texts)} textos ({input_type}), {tokens} tokens")
        return [d["embedding"] for d in dados]


def vector_literal(vetor: list[float]) -> str:
    """Formato de entrada do tipo `vector` do pgvector: '[0.1,0.2,...]'.

    Texto e não um tipo SQLAlchemy porque a coluna não é mapeada no ORM (ver
    ProductAlias) — o pacote `pgvector` seria outra dependência para formatar
    uma lista."""
    return "[" + ",".join(f"{x:.7g}" for x in vetor) + "]"
