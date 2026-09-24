"""Marca e produto nunca medem a mesma coisa.

A regra, de 24/09/2026: o catálogo tem UM produto — `coffee`, nunca `Juan
Valdez coffee` — e a marca é vínculo da análise. Mas "juan valdez café" e
"café" são buscas diferentes no Google Ads, então o vocabulário tem dono:
(produto, marca, país).

O que isso conserta está medido. Com a marca particionando a identidade, o
mesmo café existia em duas entidades e três análises geraram coleta na
DataForSEO em BOL, BRA e COL DUAS VEZES — cada entidade comprava o próprio
vocabulário. E o resolvedor trocava os dois: pedir `coffee` devolvia a marca e
pedir `Juan Valdez coffee` devolvia a genérica.

O que a partição PROTEGIA continua protegido: medido em 27/08/2026, sem
separação Cardhu e Glenfiddich dividiam a mesma entidade e a segunda herdava a
demanda da primeira.
"""

import uuid
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.product_repository import ProductRepository
from app.services.analysis_service import AnalysisService
from app.services.catalog_service import CatalogService


def _sem_match() -> AsyncMock:
    matcher = AsyncMock()
    matcher.pick = AsyncMock(return_value=None)
    return matcher


def _mercado(termo: str) -> dict:
    return {"BRA": {"primary_name": termo, "search_terms": [termo], "confidence": 0.9}}


async def test_um_produto_para_a_marca_e_para_o_generico(db_session: AsyncSession) -> None:
    """A inversão medida em 24/09/2026 não pode voltar: os dois textos caem no
    mesmo produto, e é isso que impede a mesma demanda de ser comprada duas
    vezes."""
    sufixo = uuid.uuid4().hex[:8]
    service = CatalogService(db_session, matcher=_sem_match())
    nome = f"coffee {sufixo}"

    generico = await service.resolve(nome, source="test")
    com_marca = await service.resolve(nome, source="test", brand=f"JuanValdez{sufixo}")

    assert com_marca.entity_key == generico.entity_key
    assert com_marca.canonical_title == nome, "o catálogo guarda o produto"
    assert com_marca.brand == f"JuanValdez{sufixo}", "e a marca vem ao lado"


async def test_a_analise_de_marca_mede_os_termos_da_marca(db_session: AsyncSession) -> None:
    """O requisito central: "Juan Valdez coffee" e "coffee" nunca podem dar o
    mesmo MOS, porque a busca no Google Ads é outra."""
    sufixo = uuid.uuid4().hex[:8]
    service = CatalogService(db_session, matcher=_sem_match())
    repo = ProductRepository(db_session)
    nome = f"coffee {sufixo}"

    await service.resolve(nome, source="test", localized_markets=_mercado(f"cafe {sufixo}"))
    await service.resolve(
        nome, source="test", brand=f"JuanValdez{sufixo}", localized_markets=_mercado(f"juan valdez {sufixo}")
    )

    produto = await repo.get_by_entity_key((await service.resolve(nome, source="test")).entity_key)
    marca = await repo.get_or_create_brand(name=f"JuanValdez{sufixo}", source="test")
    servico = AnalysisService(db_session)

    do_produto = await servico.vocabulario(produto, None)
    da_marca = await servico.vocabulario(produto, marca)

    assert do_produto["BRA"]["search_terms"] == [f"cafe {sufixo}"]
    assert da_marca["BRA"]["search_terms"] == [f"juan valdez {sufixo}"]


async def test_o_campeao_de_uma_marca_nao_vira_o_da_outra(db_session: AsyncSession) -> None:
    """A contaminação que se autoconfirma: campeão conhecido faz a análise PULAR
    a coleta dos mercados de referência, então um campeão herdado nunca seria
    corrigido — existir já impede a medição que o desmentiria."""
    sufixo = uuid.uuid4().hex[:8]
    service = CatalogService(db_session, matcher=_sem_match())
    repo = ProductRepository(db_session)
    nome = f"whisky {sufixo}"

    resolvido = await service.resolve(nome, source="test", brand=f"Cardhu{sufixo}")
    produto = await repo.get_by_entity_key(resolvido.entity_key)
    cardhu = await repo.get_or_create_brand(name=f"Cardhu{sufixo}", source="test")
    glen = await repo.get_or_create_brand(name=f"Glen{sufixo}", source="test")

    await repo.set_champion(produto, country="GBR", mos=None, period_key="2026-09", brand_key=cardhu.brand_key)
    await db_session.flush()

    ja_tem, pais, _ = await repo.champion_for(produto, cardhu.brand_key, "2026-09")
    assert (ja_tem, pais) == (True, "GBR")

    da_outra, _, _ = await repo.champion_for(produto, glen.brand_key, "2026-09")
    assert da_outra is False, "a outra marca ainda não tem campeão"

    do_produto, _, _ = await repo.champion_for(produto, None, "2026-09")
    assert do_produto is False, "nem o produto herda o campeão de uma marca"
