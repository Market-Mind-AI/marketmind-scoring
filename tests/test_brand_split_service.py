"""Reparo do estado legado: entidade com título genérico e vocabulário de marca.

O caso real, medido no banco vivo em 27/08/2026: a entidade `single malt Scotch
whisky` guardava 24 mercados de vocabulário 100% Cardhu e os aliases `cardhu`,
`cardhu whisky`, `cardhu gold reserve`. A partição da identidade (migration 031)
impede que isso volte a acontecer e não desfaz o que já aconteceu.

Não dá para deixar a resolução consertar sozinha: a resolução da família cairia
justamente na entidade poluída — ela é dona do alias genérico — e a categoria
passaria a ser medida com os termos da marca, o que é o mesmo erro de outro
ângulo.
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


async def test_split_produz_familia_limpa_marca_e_aposentada(db_session: AsyncSession) -> None:
    sufixo = uuid.uuid4().hex[:8]
    entity_key, titulo = await _entidade_poluida(db_session, sufixo)
    repo = ProductRepository(db_session)

    report = await BrandSplitService(db_session).split(entity_key=entity_key, brand=f"Cardhu{sufixo}")

    marca = await repo.get_by_entity_key(report.brand_entity_key)
    familia = await repo.get_by_entity_key(report.generic_entity_key)
    aposentada = await repo.get_by_entity_key(entity_key)

    # A marca leva TODO o vocabulário curado — nada é recoletado.
    assert marca.canonical_title == f"Cardhu{sufixo} {titulo}"
    assert marca.brand_key is not None
    assert marca.generic_entity_key == familia.entity_key
    assert sorted((marca.localized_markets or {}).keys()) == ["ESP", "GBR"]
    assert marca.target_age_min == 25 and marca.category == "Spirits"
    # E o campeão, que foi eleito com a demanda da marca.
    assert marca.champion_country == "GBR" and marca.champion_period_key == "2026-08"

    # A família nasce LIMPA: é ela que o enriquecimento vai preencher com termos
    # sem marca, e contra ela que o share da marca é lido.
    assert familia.canonical_title == titulo
    assert familia.brand_key is None and familia.generic_entity_key is None
    assert not familia.localized_markets

    # A original é aposentada, não apagada: o histórico já entregue continua
    # pendurado nela e segue abrindo.
    assert aposentada.merged_into == marca.entity_key
    assert not aposentada.localized_markets


async def test_alias_generico_fica_com_a_familia_e_o_resto_com_a_marca(db_session: AsyncSession) -> None:
    """A regra que torna o reparo automatizável: o alias igual ao título
    normalizado da família é da família; todo o resto pertencia ao vocabulário
    poluído, que é da marca. Nada é descartado."""
    sufixo = uuid.uuid4().hex[:8]
    entity_key, titulo = await _entidade_poluida(db_session, sufixo)
    repo = ProductRepository(db_session)

    report = await BrandSplitService(db_session).split(entity_key=entity_key, brand=f"Cardhu{sufixo}")

    donos = await repo.existing_alias_owners(
        {
            normalize_alias(titulo),
            normalize_alias(f"cardhu {sufixo}"),
            normalize_alias(f"cardhu gold {sufixo}"),
            normalize_alias(f"whisky cardhu {sufixo}"),
        }
    )
    assert donos[normalize_alias(titulo)] == report.generic_entity_key
    for termo in (f"cardhu {sufixo}", f"cardhu gold {sufixo}", f"whisky cardhu {sufixo}"):
        assert donos[normalize_alias(termo)] == report.brand_entity_key

    # A chave de identidade `marca + nome` passa a existir apontando para a
    # entidade da marca. Sem ela o reparo deixaria a próxima resolução cair no
    # passo 2/3 e CRIAR DUPLICATA — o erro que ele existe para consertar.
    chave_da_marca = normalize_alias(f"Cardhu{sufixo} {titulo}")
    donos_chave = await repo.existing_alias_owners({chave_da_marca})
    assert donos_chave[chave_da_marca] == report.brand_entity_key

    assert report.aliases_para_familia == 1  # o genérico já existia
    assert report.aliases_para_marca == 4  # 3 movidos + a chave `marca + nome`
    # Nenhum alias sobra na entidade aposentada.
    aposentada = await repo.get_by_entity_key(entity_key)
    assert list(aposentada.aliases) == []


async def test_termos_excluidos_seguem_o_vocabulario(db_session: AsyncSession) -> None:
    """Os `excluded_terms` são o lado negativo do MESMO vocabulário curado, então
    vão para onde o vocabulário foi. Deixá-los na entidade aposentada tiraria o
    veto do passo 2 justamente da entidade que passou a ser medida."""
    sufixo = uuid.uuid4().hex[:8]
    entity_key, _ = await _entidade_poluida(db_session, sufixo)

    report = await BrandSplitService(db_session).split(entity_key=entity_key, brand=f"Cardhu{sufixo}")

    donos = (
        (
            await db_session.execute(
                select(ProductExcludedTerm.entity_key).where(
                    ProductExcludedTerm.term_normalized == normalize_alias(f"cardhu bar {sufixo}")
                )
            )
        )
        .scalars()
        .all()
    )
    assert list(donos) == [report.brand_entity_key]
    assert report.termos_excluidos_movidos == 1


async def test_a_marca_resolve_para_a_entidade_certa_depois_do_reparo(db_session: AsyncSession) -> None:
    """O teste que fecha o caso: depois do reparo, a consulta da marca cai na
    entidade da marca pelo caminho O(1), e a consulta genérica cai na família —
    que era exatamente o que estava trocado."""
    from unittest.mock import AsyncMock

    from app.services.catalog_service import CatalogService

    sufixo = uuid.uuid4().hex[:8]
    entity_key, titulo = await _entidade_poluida(db_session, sufixo)
    report = await BrandSplitService(db_session).split(entity_key=entity_key, brand=f"Cardhu{sufixo}")

    matcher = AsyncMock()
    matcher.pick.return_value = None
    service = CatalogService(db_session, matcher=matcher)

    com_marca = await service.resolve(titulo, source="test", brand=f"Cardhu{sufixo}")
    assert com_marca.entity_key == report.brand_entity_key
    assert com_marca.generic_title == titulo

    generico = await service.resolve(titulo, source="test")
    assert generico.entity_key == report.generic_entity_key


async def test_recusa_separar_o_que_ja_esta_particionado(db_session: AsyncSession) -> None:
    """Separar duas vezes criaria uma terceira entidade e espalharia o
    vocabulário — o oposto do que a operação existe para fazer."""
    sufixo = uuid.uuid4().hex[:8]
    entity_key, _ = await _entidade_poluida(db_session, sufixo)
    service = BrandSplitService(db_session)
    report = await service.split(entity_key=entity_key, brand=f"Cardhu{sufixo}")

    with pytest.raises(MarketMindCoreError):
        await service.split(entity_key=report.brand_entity_key, brand=f"Cardhu{sufixo}")
    with pytest.raises(MarketMindCoreError):
        await service.split(entity_key=entity_key, brand=f"Cardhu{sufixo}")
