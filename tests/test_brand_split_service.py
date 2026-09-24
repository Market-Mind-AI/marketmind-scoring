"""Reparo do estado legado: entidade com título genérico e vocabulário de marca.

O caso real, medido no banco vivo em 27/08/2026: a entidade `single malt Scotch
whisky` guardava 24 mercados de vocabulário 100% Cardhu e os aliases `cardhu`,
`cardhu whisky`, `cardhu gold reserve`. O rótulo dizia a categoria e o número
media a marca.

Desde 24/09/2026 o reparo mudou de forma junto com a regra: a marca não cria
entidade, então não há o que fatiar. A entidade CONTINUA sendo o produto e o
que se move é o vocabulário, para (produto, marca, país). Não dá para deixar a
resolução consertar sozinha — ela cairia justamente nesta entidade, que é dona
do alias genérico, e a categoria seguiria medida com os termos da marca.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import MarketMindCoreError
from app.db.models.product_excluded_term import ProductExcludedTerm
from app.repositories.product_repository import ProductRepository
from app.services.brand_split_service import BrandSplitService
from app.utils.text import normalize_alias


async def _entidade_poluida(session: AsyncSession, sufixo: str) -> tuple[uuid.UUID, str]:
    """Reproduz o estado legado: título genérico, vocabulário de marca, alias
    genérico e aliases de marca, mais um termo excluído."""
    repo = ProductRepository(session)
    titulo = f"single malt scotch whisky {sufixo}"
    entity = await repo.create_entity(
        canonical_title=titulo,
        category="Spirits",
        localized_markets={
            "GBR": {"primary_name": f"cardhu {sufixo}", "search_terms": [f"cardhu {sufixo}", f"cardhu gold {sufixo}"]},
            "ESP": {"primary_name": f"whisky cardhu {sufixo}", "search_terms": [f"whisky cardhu {sufixo}"]},
        },
        reference_candidates=[{"country": "GBR", "search_terms": [f"cardhu {sufixo}"]}],
        target_age_min=25,
        target_age_max=65,
    )
    await repo.create_alias(alias_normalized=normalize_alias(titulo), entity_key=entity.entity_key, source="analyze")
    await repo.create_aliases_bulk(
        aliases={
            normalize_alias(f"cardhu {sufixo}"): entity.entity_key,
            normalize_alias(f"cardhu gold {sufixo}"): entity.entity_key,
            normalize_alias(f"whisky cardhu {sufixo}"): entity.entity_key,
        },
        source="localized_market",
    )
    await repo.create_excluded_terms_bulk(
        entity_key=entity.entity_key, termos={normalize_alias(f"cardhu bar {sufixo}"): "cardhu bar"}, source="test"
    )
    await repo.set_champion(entity, country="GBR", mos=66.9, period_key="2026-08")
    await session.commit()
    return entity.entity_key, titulo


async def test_o_vocabulario_vai_para_a_marca_e_o_produto_fica_limpo(db_session: AsyncSession) -> None:
    """Uma entidade só, depois do reparo: o produto. Os termos que mediam a
    marca passam a ter dono declarado, e o produto volta a não ter vocabulário
    nenhum — que é o estado certo até alguém enriquecer a categoria."""
    sufixo = uuid.uuid4().hex[:8]
    entity_key, titulo = await _entidade_poluida(db_session, sufixo)
    repo = ProductRepository(db_session)

    report = await BrandSplitService(db_session).split(entity_key=entity_key, brand=f"Cardhu{sufixo}")

    assert report.entity_key == entity_key, "não se cria entidade nova"
    assert report.mercados_movidos == ["ESP", "GBR"]

    produto = await repo.get_by_entity_key(entity_key)
    assert produto.merged_into is None, "o produto não é aposentado"
    assert produto.canonical_title == titulo
    assert not produto.localized_markets and not produto.reference_candidates

    marca = await repo.get_or_create_brand(name=f"Cardhu{sufixo}", source="test")
    da_marca = await repo.markets_for(produto, marca.brand_key)
    assert sorted(da_marca) == ["ESP", "GBR"]
    assert f"cardhu {sufixo}" in da_marca["GBR"]["search_terms"]


async def test_o_campeao_da_marca_nao_e_herdado_pelo_produto(db_session: AsyncSession) -> None:
    """Ele foi eleito com a demanda da marca. Herdado, valeria para a categoria
    inteira — e pior: faria a próxima análise PULAR a coleta que o corrigiria,
    porque campeão conhecido é o sinal de que não há o que recolher."""
    sufixo = uuid.uuid4().hex[:8]
    entity_key, _ = await _entidade_poluida(db_session, sufixo)

    await BrandSplitService(db_session).split(entity_key=entity_key, brand=f"Cardhu{sufixo}")

    produto = await ProductRepository(db_session).get_by_entity_key(entity_key)
    assert produto.champion_country is None and produto.champion_period_key is None


async def test_o_alias_generico_continua_apontando_para_o_produto(db_session: AsyncSession) -> None:
    """Sem ele a próxima resolução do nome genérico cria duplicata — o erro que
    este reparo existe para consertar."""
    sufixo = uuid.uuid4().hex[:8]
    entity_key, titulo = await _entidade_poluida(db_session, sufixo)

    await BrandSplitService(db_session).split(entity_key=entity_key, brand=f"Cardhu{sufixo}")

    donos = await ProductRepository(db_session).existing_alias_owners({normalize_alias(titulo)})
    assert donos[normalize_alias(titulo)] == entity_key


async def test_os_termos_excluidos_ficam_com_o_produto(db_session: AsyncSession) -> None:
    """Eles vetam a fusão de produtos VIZINHOS — `cardhu bar` não é whisky —, e
    essa é uma pergunta do produto, não da marca."""
    sufixo = uuid.uuid4().hex[:8]
    entity_key, _ = await _entidade_poluida(db_session, sufixo)

    await BrandSplitService(db_session).split(entity_key=entity_key, brand=f"Cardhu{sufixo}")

    restantes = await db_session.execute(
        select(ProductExcludedTerm).where(ProductExcludedTerm.entity_key == entity_key)
    )
    assert len(list(restantes.scalars().all())) == 1


async def test_recusa_separar_o_que_ja_foi_aposentado(db_session: AsyncSession) -> None:
    sufixo = uuid.uuid4().hex[:8]
    entity_key, _ = await _entidade_poluida(db_session, sufixo)
    repo = ProductRepository(db_session)
    produto = await repo.get_by_entity_key(entity_key)
    outra = await repo.create_entity(canonical_title=f"outra {sufixo}")
    produto.merged_into = outra.entity_key
    await db_session.commit()

    with pytest.raises(MarketMindCoreError, match="aposentada"):
        await BrandSplitService(db_session).split(entity_key=entity_key, brand=f"Cardhu{sufixo}")


async def test_recusa_marca_sem_forma_normalizada(db_session: AsyncSession) -> None:
    sufixo = uuid.uuid4().hex[:8]
    entity_key, _ = await _entidade_poluida(db_session, sufixo)

    with pytest.raises(MarketMindCoreError, match="forma normalizada"):
        await BrandSplitService(db_session).split(entity_key=entity_key, brand="!!!")
