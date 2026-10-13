"""Passo 3a: busca semântica que escolhe os candidatos do LLM (migration 052).

Os vetores aqui são de mentira e de propósito: um eixo por CONCEITO. O que se
testa é o encanamento — HNSW, partição de marca, agrupamento por entidade,
ordem na lista do LLM —, não a qualidade do modelo de embeddings, que é medida
contra o catálogo real em scripts/benchmark_semantic_recall.py.
"""

import json
import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.repositories.product_repository import ProductRepository
from app.services.catalog_service import CatalogService
from app.services.embedding_client import EmbeddingClient, EmbeddingError, vector_literal

_DIM = settings.voyage_embedding_dimensions


def _eixo(i: int) -> list[float]:
    v = [0.0] * _DIM
    v[i % _DIM] = 1.0
    return v


class _Embedder(EmbeddingClient):
    """Mesmo conceito, mesmo eixo; o resto cai num eixo derivado do texto, longe de tudo."""

    def __init__(self, conceitos: dict[str, int]) -> None:
        super().__init__()
        self._conceitos = conceitos
        self.chamadas: list[tuple[list[str], str]] = []

    @property
    def enabled(self) -> bool:
        return True

    async def embed(self, texts: list[str], *, input_type: str) -> list[list[float]]:  # type: ignore[override]
        self.chamadas.append((texts, input_type))
        return [
            _eixo(self._conceitos.get(t, 100 + sum(map(ord, t)) % (_DIM - 100))) for t in texts
        ]


def _captura() -> tuple[AsyncMock, list[list[str]]]:
    vistos: list[list[str]] = []

    def _pick(*, text, candidates, motivo=None, category=None):  # noqa: ANN001, ANN202
        vistos.append([c.canonical_title for c in candidates])
        return None

    matcher = AsyncMock()
    matcher.pick.side_effect = _pick
    return matcher, vistos


async def _vetores_pendentes(session: AsyncSession) -> int:
    return (await session.execute(text("SELECT count(*) FROM product_aliases WHERE embedding IS NULL"))).scalar_one()


async def test_vizinho_semantico_entra_na_lista_que_a_recencia_deixava_de_fora(db_session: AsyncSession) -> None:
    """O caso que motivou o passo: `drone` existe, mas é a entidade mais ANTIGA
    da categoria e o teto da lista só alcança as mais recentes. Sem o vetor, o
    LLM nunca a vê, e `quadcopter` vira duplicata."""
    sufixo = uuid.uuid4().hex[:8]
    drone = f"drone {sufixo}"
    embedder = _Embedder({drone: 7, "quadcopter": 7})
    matcher, vistos = _captura()
    service = CatalogService(db_session, matcher=matcher, embedder=embedder)

    await service.resolve(drone, source="test", category="Electronics")
    for _ in range(settings.catalog_match_candidate_limit + 3):
        await service.resolve(uuid.uuid4().hex, source="test", category="Electronics")
    assert await _vetores_pendentes(db_session) == 0

    await service.resolve("quadcopter", source="test", category="Electronics")
    assert vistos[-1][0] == drone

    # Sem a busca semântica, a mesma consulta não mostra o drone ao LLM.
    sem_vetor = CatalogService(db_session, matcher=matcher, embedder=EmbeddingClient())
    await sem_vetor.resolve("multirotor", source="test", category="Electronics")
    assert drone not in vistos[-1]


async def test_busca_semantica_respeita_particao_de_marca_e_entidade_fundida(db_session: AsyncSession) -> None:
    repo = ProductRepository(db_session)
    marca = await repo.get_or_create_brand(name=f"marca {uuid.uuid4().hex[:6]}")
    generica = await repo.create_entity(canonical_title=f"generica {uuid.uuid4()}")
    da_marca = await repo.create_entity(
        canonical_title=f"da marca {uuid.uuid4()}", brand_key=marca.brand_key, generic_entity_key=generica.entity_key
    )
    fundida = await repo.create_entity(canonical_title=f"fundida {uuid.uuid4()}")
    fundida.merged_into = generica.entity_key
    for entidade in (generica, da_marca, fundida):
        await repo.set_alias(
            alias_normalized=f"termo {entidade.entity_key}", entity_key=entidade.entity_key, source="test"
        )
    await db_session.flush()
    pendentes = await repo.aliases_without_embedding(model="m", limit=10)
    await repo.save_alias_embeddings([(id_, vector_literal(_eixo(3))) for id_, _ in pendentes], model="m")

    consulta = vector_literal(_eixo(3))
    genericos = await repo.semantic_search(consulta, model="m", brand_key=None, top_k=10)
    da_marca_achados = await repo.semantic_search(consulta, model="m", brand_key=marca.brand_key, top_k=10)
    outro_modelo = await repo.semantic_search(consulta, model="outro", brand_key=None, top_k=10)

    assert [e.entity_key for e, _ in genericos] == [generica.entity_key]
    assert genericos[0][1] == pytest.approx(0.0, abs=1e-6)
    assert [e.entity_key for e, _ in da_marca_achados] == [da_marca.entity_key]
    # Vetor de outro modelo não está no mesmo espaço e não é comparado.
    assert outro_modelo == []


async def test_backfill_vetoriza_tudo_e_e_idempotente(db_session: AsyncSession) -> None:
    repo = ProductRepository(db_session)
    for _ in range(5):
        entidade = await repo.create_entity(canonical_title=f"produto {uuid.uuid4()}")
        await repo.set_alias(alias_normalized=f"alias {uuid.uuid4()}", entity_key=entidade.entity_key, source="test")
    await db_session.commit()

    embedder = _Embedder({})
    service = CatalogService(db_session, matcher=AsyncMock(), embedder=embedder)
    primeira = await service.backfill_embeddings(batch_size=2)
    segunda = await service.backfill_embeddings(batch_size=2)

    assert primeira["aliases"] == 5 and primeira["lotes"] == 3
    assert segunda["aliases"] == 0
    assert await _vetores_pendentes(db_session) == 0
    assert all(tipo == "document" for _, tipo in embedder.chamadas)


async def test_voyage_fora_nao_derruba_a_resolucao(db_session: AsyncSession) -> None:
    class _Fora(_Embedder):
        async def embed(self, texts, *, input_type):  # noqa: ANN001, ANN202
            raise EmbeddingError("503")

    matcher, vistos = _captura()
    service = CatalogService(db_session, matcher=matcher, embedder=_Fora({}))
    await service.resolve(f"existente {uuid.uuid4()}", source="test")
    resultado = await service.resolve(f"outra coisa {uuid.uuid4()}", source="test")

    assert resultado.method == "created"
    assert vistos and vistos[-1]  # a seleção antiga continuou alimentando o LLM


async def test_cliente_voyage_ordena_por_indice_e_conta_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "voyage_api_key", "k")
    pedidos: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        corpo = json.loads(request.content)
        pedidos.append(corpo)
        dados = [{"index": i, "embedding": _eixo(i)} for i in range(len(corpo["input"]))]
        return httpx.Response(200, json={"data": list(reversed(dados)), "usage": {"total_tokens": 9}})

    cliente = EmbeddingClient(transport=httpx.MockTransport(_handler))
    vetores = await cliente.embed(["a", "b"], input_type="query")

    assert vetores == [_eixo(0), _eixo(1)]
    assert cliente.tokens == 9
    assert pedidos[0]["input_type"] == "query"
    assert pedidos[0]["output_dimension"] == _DIM


async def test_cliente_voyage_recusa_dimensao_errada(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "voyage_api_key", "k")
    transporte = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}], "usage": {}})
    )
    with pytest.raises(EmbeddingError):
        await EmbeddingClient(transport=transporte).embed(["a"], input_type="document")
