"""Passo 3a: busca semântica que escolhe os candidatos do LLM (migration 052).

Os vetores aqui são de mentira e de propósito: um eixo por CONCEITO. O que se
testa é o encanamento — HNSW, partição de marca, agrupamento por entidade,
ordem na lista do LLM —, não a qualidade do modelo de embeddings, que é medida
contra o catálogo real em scripts/benchmark_semantic_recall.py.
"""

import json
import uuid
from types import SimpleNamespace
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

    def _pick(*, text, candidates, motivo=None, category=None, evidencias=None):  # noqa: ANN001, ANN202
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
    pendentes = await repo.pending_embeddings(model="m", limit=10)
    await repo.save_embeddings([(t, id_, vector_literal(_eixo(3))) for t, id_, _ in pendentes], model="m")

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


# --- Veto semântico por produto antagônico (migration 053) ---------------------------------------

_AIR = 11  # eixo "air fryer"
_OLEO = 12  # eixo "fritadeira a óleo"


def _mercados_air_fryer() -> dict:
    return {
        "BRA": {
            "primary_name": "fritadeira sem oleo eletrica",
            "search_terms": ["fritadeira sem oleo eletrica"],
            "excluded_terms": ["receita", "fritadeira a oleo"],
            "antagonist_terms": ["fritadeira a oleo", "tacho de fritura"],
            "confidence": 0.9,
        }
    }


async def _air_fryer(db_session: AsyncSession, embedder: _Embedder, matcher: AsyncMock) -> CatalogService:
    service = CatalogService(db_session, matcher=matcher, embedder=embedder)
    await service.resolve(
        "fritadeira sem oleo eletrica", source="test", category="Kitchen", localized_markets=_mercados_air_fryer()
    )
    return service


async def test_antagonico_e_indexado_vetorizado_e_promove_o_excluido(db_session: AsyncSession) -> None:
    embedder = _Embedder({"fritadeira sem oleo eletrica": _AIR, "fritadeira a oleo": _OLEO, "tacho de fritura": _OLEO})
    matcher, _ = _captura()
    await _air_fryer(db_session, embedder, matcher)

    linhas = (
        await db_session.execute(
            text("SELECT term_normalized, kind, embedding IS NOT NULL FROM product_excluded_terms ORDER BY 1")
        )
    ).all()
    assert linhas == [
        ("fritadeira a oleo", "antagonist", True),  # estava nas duas listas: promovido
        ("receita", "excluded", False),  # termo de uso nunca ganha vetor
        ("tacho de fritura", "antagonist", True),
    ]


async def test_passo_2_vetado_pelo_significado_vai_ao_llm_com_a_evidencia(db_session: AsyncSession) -> None:
    """A forma casa com a air fryer (trigram alto contra o alias) e nenhum
    excluded_term a veta por forma (mesmas palavras, outra ordem: o trigram
    aceitaria); o significado está no eixo do antagônico."""
    consulta = "fritadeira eletrica sem oleo"
    embedder = _Embedder(
        {"fritadeira sem oleo eletrica": _AIR, "fritadeira a oleo": _OLEO, "tacho de fritura": _OLEO, consulta: _OLEO}
    )
    matcher, vistos = _captura()
    service = await _air_fryer(db_session, embedder, matcher)

    resultado = await service.resolve(consulta, source="test", category="Kitchen")

    assert resultado.method == "created"  # o LLM (dublê) não aceitou; nada foi fundido pelo trigram
    assert vistos[-1][0] == "fritadeira sem oleo eletrica"
    evidencias = matcher.pick.call_args.kwargs["evidencias"]
    [texto] = evidencias.values()
    assert "NOT" in texto and "fritadeira a oleo" in texto


async def test_passo_2_aceita_quando_o_significado_concorda(db_session: AsyncSession) -> None:
    consulta = "fritadeira eletrica sem oleo"
    embedder = _Embedder(
        {"fritadeira sem oleo eletrica": _AIR, "fritadeira a oleo": _OLEO, "tacho de fritura": _OLEO, consulta: _AIR}
    )
    matcher, _ = _captura()
    service = await _air_fryer(db_session, embedder, matcher)

    resultado = await service.resolve(consulta, source="test", category="Kitchen")

    assert resultado.method == "trigram"
    assert resultado.canonical_title == "fritadeira sem oleo eletrica"


async def test_entidade_sem_antagonico_nao_paga_embedding_no_passo_2(db_session: AsyncSession) -> None:
    embedder = _Embedder({})
    matcher, _ = _captura()
    service = CatalogService(db_session, matcher=matcher, embedder=embedder)
    await service.resolve("panela de pressao eletrica", source="test")
    embedder.chamadas.clear()

    resultado = await service.resolve("panela eletrica de pressao", source="test")

    assert resultado.method == "trigram"
    assert not [c for c in embedder.chamadas if c[1] == "query"]


async def test_backfill_de_termos_nao_poda_antagonico(db_session: AsyncSession) -> None:
    embedder = _Embedder({})
    matcher, _ = _captura()
    service = await _air_fryer(db_session, embedder, matcher)

    relatorio = await service.backfill_terms()

    assert relatorio["obsoletos"] == 0
    kinds = (await db_session.execute(text("SELECT kind, count(*) FROM product_excluded_terms GROUP BY 1"))).all()
    assert dict(kinds) == {"antagonist": 2, "excluded": 1}


async def test_backfill_de_antagonicos_preenche_indexa_e_e_idempotente(db_session: AsyncSession) -> None:
    class _Cliente:
        def __init__(self) -> None:
            self.pedidos: list[list[tuple]] = []

        async def localized_markets(self, *, product_name, brand, category, countries):  # noqa: ANN001, ANN201
            self.pedidos.append(countries)
            return [
                SimpleNamespace(country=iso3, antagonist_terms=["fritadeira a oleo"], search_terms=["outro termo"])
                for iso3, _ in countries
            ]

    mercados = _mercados_air_fryer()
    del mercados["BRA"]["antagonist_terms"]
    mercados["BRA"]["excluded_terms"] = ["receita"]
    embedder = _Embedder({})
    service = CatalogService(db_session, matcher=_captura()[0], embedder=embedder)
    await service.resolve("fritadeira sem oleo eletrica", source="test", localized_markets=mercados)

    cliente = _Cliente()
    primeiro = await service.backfill_antagonists(cliente)  # type: ignore[arg-type]
    segundo = await service.backfill_antagonists(cliente)  # type: ignore[arg-type]

    assert primeiro["entidades"] == 1 and primeiro["antagonicos"] == 1
    assert segundo["entidades"] == 0 and segundo["puladas"] == 1
    assert cliente.pedidos == [[("BRA", None)]]
    # Da resposta só sai o antagônico: o vocabulário de busca curado não muda.
    [entidade] = (await db_session.execute(text("SELECT localized_markets FROM product_catalog"))).scalars()
    assert entidade["BRA"]["search_terms"] == ["fritadeira sem oleo eletrica"]
    consulta = text("SELECT term_normalized, kind FROM product_excluded_terms ORDER BY 1")
    kinds = (await db_session.execute(consulta)).all()
    assert kinds == [("fritadeira a oleo", "antagonist"), ("receita", "excluded")]


async def test_match_do_llm_traz_antagonicos_no_idioma_da_consulta_sem_chamada_extra(db_session: AsyncSession) -> None:
    """O alias aprendido pelo passo 3 (francês, idioma que o catálogo não tinha)
    chega com o antagônico no mesmo idioma, tirado da MESMA resposta do LLM."""

    def _pick(*, text, candidates, motivo=None, category=None, evidencias=None):  # noqa: ANN001, ANN202
        motivo["relationship"] = "same"
        motivo["reasoning"] = "tradução"
        motivo["antagonist_terms"] = ["écouteurs filaires"]
        return candidates[0]

    matcher = AsyncMock()
    matcher.pick.side_effect = _pick
    embedder = _Embedder({})
    service = CatalogService(db_session, matcher=_captura()[0], embedder=embedder)
    await service.resolve("wireless earbuds", source="test")
    service._matcher = matcher

    resultado = await service.resolve("écouteurs sans fil", source="test")

    assert resultado.method == "llm"
    assert matcher.pick.call_count == 1
    linhas = (
        await db_session.execute(
            text("SELECT term_normalized, kind, source, embedding IS NOT NULL FROM product_excluded_terms")
        )
    ).all()
    assert linhas == [("ecouteurs filaires", "antagonist", "catalog_match", True)]
    # A poda do backfill só toca no que veio da curadoria; o aprendido fica.
    assert (await service.backfill_terms())["obsoletos"] == 0


async def test_passo_2_abstem_na_banda_e_manda_ao_llm_com_evidencia(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Consulta a meio caminho entre o produto e o antagônico: nenhum lado
    ganha por margem, o trigram não decide e o LLM recebe a dúvida escrita."""
    consulta = "fritadeira eletrica sem oleo"

    class _Meio(_Embedder):
        async def embed(self, texts, *, input_type):  # type: ignore[override]  # noqa: ANN001
            vetores = await super().embed(texts, input_type=input_type)
            if input_type == "query":
                meio = [0.0] * _DIM
                meio[_AIR] = 0.75
                meio[_OLEO] = 0.65
                return [meio for _ in texts]
            return vetores

    # O alias que a primeira resolução grava também fica no eixo do produto;
    # sem isso ele cairia num eixo aleatório e puxaria a média k-NN para longe.
    embedder = _Meio(
        {
            "fritadeira sem oleo eletrica": _AIR,
            consulta: _AIR,
            "fritadeira a oleo": _OLEO,
            "tacho de fritura": _OLEO,
        }
    )
    matcher, vistos = _captura()
    service = await _air_fryer(db_session, embedder, matcher)

    monkeypatch.setattr(settings, "catalog_semantic_abstention_margin", 0.0)
    resultado = await service.resolve(consulta, source="test", category="Kitchen")
    assert resultado.method == "trigram"  # sem banda, o lado do produto ganha por pouco e o passo 2 aceita

    monkeypatch.setattr(settings, "catalog_semantic_abstention_margin", 0.2)
    resultado = await service.resolve(consulta + " 5l", source="test", category="Kitchen")
    assert resultado.method == "created"  # abstido: o LLM (dublê) recebeu e não aceitou
    [texto] = matcher.pick.call_args.kwargs["evidencias"].values()
    assert "cannot tell which side" in texto


async def test_polaridade_usa_a_media_dos_k_mais_proximos(db_session: AsyncSession) -> None:
    """Um antagônico isolado perto da consulta não veta sozinho se os demais
    estão longe e o produto tem vários termos perto: k-NN, não 1-NN."""
    repo = ProductRepository(db_session)
    entidade = await repo.create_entity(canonical_title=f"produto {uuid.uuid4()}")
    for i in range(3):
        await repo.set_alias(
            alias_normalized=f"termo {i} {entidade.entity_key}", entity_key=entidade.entity_key, source="t"
        )
    await repo.create_excluded_terms_bulk(
        entity_key=entidade.entity_key,
        termos={f"antag {i}": f"antag {i}" for i in range(3)},
        source="t",
        kind="antagonist",
    )
    await db_session.flush()
    pendentes = await repo.pending_embeddings(model="m", limit=10)
    vetores = []
    for tabela, id_, texto in pendentes:
        if tabela == "product_aliases":
            v = _eixo(1)  # os três termos do produto, todos no eixo da consulta
        elif texto == "antag 0":
            v = _eixo(1)  # UM antagônico idêntico à consulta
        else:
            v = _eixo(50)  # os outros dois, longe
        vetores.append((tabela, id_, vector_literal(v)))
    await repo.save_embeddings(vetores, model="m")

    polaridades = await repo.polarity(vector_literal(_eixo(1)), model="m", entity_keys=[entidade.entity_key])
    [polaridade] = polaridades.values()

    assert polaridade.distancia_antagonico == pytest.approx(0.0, abs=1e-6)  # o 1-NN empataria e vetaria
    assert polaridade.escore > 0 and not polaridade.vetada  # a média dos 3 decide pelo produto
