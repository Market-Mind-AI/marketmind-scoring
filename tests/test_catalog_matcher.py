"""CatalogMatcher é o que separa uma resolução correta de uma fusão
permanente de dois produtos, então os testes são escritos em torno da
assimetria dos dois erros: dizer "é o mesmo" errado corrompe o catálogo em
silêncio, dizer "é diferente" errado só cria uma duplicata. Todo caminho
ambíguo ou degradado precisa terminar em "sem correspondência".
"""

import uuid

import pytest
from pydantic import BaseModel

from app.services.catalog_matcher import CatalogMatcher, CatalogMatchDecision
from app.services.llm.base import LLMError


class _Produto:
    def __init__(self, titulo: str, categoria: str | None = None, mercados: dict | None = None) -> None:
        self.entity_key = uuid.uuid4()
        self.canonical_title = titulo
        self.category = categoria
        self.localized_markets = mercados


class _Provider:
    """Captura o prompt para os testes poderem afirmar sobre o que o modelo vê,
    e devolve uma decisão pré-definida."""

    def __init__(self, decisao: BaseModel | Exception) -> None:
        self._decisao = decisao
        self.system: str | None = None
        self.user: str | None = None

    async def complete(self, *, system=None, user=None, image=None, response_model=None):
        self.system = system
        self.user = user
        if isinstance(self._decisao, Exception):
            raise self._decisao
        return self._decisao


@pytest.mark.asyncio
async def test_escolhe_a_entidade_que_o_modelo_nomeia() -> None:
    air = _Produto("air fryer")
    oleo = _Produto("deep fryer")
    provider = _Provider(CatalogMatchDecision(entity_key=str(air.entity_key), reasoning="mesmo aparelho"))

    escolhido = await CatalogMatcher(provider=provider).pick(
        text="freidora de aire", candidates=[air, oleo]
    )

    assert escolhido is air


@pytest.mark.asyncio
async def test_entity_key_vazia_significa_criar_nova() -> None:
    """Abstenção do modelo tem que virar entidade nova, nunca cair para o
    candidato mais parecido."""
    air = _Produto("air fryer")
    provider = _Provider(CatalogMatchDecision(entity_key="", reasoning="função diferente"))

    assert await CatalogMatcher(provider=provider).pick(text="freidora de aceite", candidates=[air]) is None


@pytest.mark.asyncio
async def test_chave_alucinada_nao_vira_o_candidato_mais_proximo() -> None:
    """Se o modelo devolve uma entity_key fora da lista, o resultado é "sem
    correspondência". Cair para "o mais parecido" reintroduziria a decisão por
    semelhança que este passo existe para substituir."""
    air = _Produto("air fryer")
    provider = _Provider(CatalogMatchDecision(entity_key=str(uuid.uuid4())))

    assert await CatalogMatcher(provider=provider).pick(text="qualquer coisa", candidates=[air]) is None


@pytest.mark.asyncio
async def test_llm_indisponivel_falha_para_sem_correspondencia() -> None:
    """Um LLM fora do ar nunca pode causar uma fusão."""
    provider = _Provider(LLMError("provider fora do ar"))

    assert await CatalogMatcher(provider=provider).pick(
        text="air fryer", candidates=[_Produto("air fryer")]
    ) is None


@pytest.mark.asyncio
async def test_sem_candidatos_nao_gasta_chamada() -> None:
    provider = _Provider(CatalogMatchDecision(entity_key="x"))

    assert await CatalogMatcher(provider=provider).pick(text="air fryer", candidates=[]) is None
    assert provider.user is None, "sem candidatos não há o que decidir — não gastar uma chamada"


@pytest.mark.asyncio
async def test_prompt_mostra_os_termos_curados_e_nao_so_o_titulo() -> None:
    """Os `search_terms` são a evidência real de identidade; o
    `canonical_title` é só o idioma de quem cadastrou primeiro. Sem os termos,
    o modelo teria que adivinhar a partir de uma palavra."""
    air = _Produto(
        "air fryer",
        categoria="Kitchen Appliances",
        mercados={
            "ESP": {"search_terms": ["freidora de aire", "freidora sin aceite"]},
            "BRA": {"search_terms": ["fritadeira sem óleo"]},
        },
    )
    provider = _Provider(CatalogMatchDecision(entity_key=""))

    await CatalogMatcher(provider=provider).pick(text="heteluchtfriteuse", candidates=[air])

    assert "freidora de aire" in provider.user
    assert "fritadeira sem óleo" in provider.user
    assert "Kitchen Appliances" in provider.user
    assert str(air.entity_key) in provider.user
    assert "heteluchtfriteuse" in provider.user
