"""O esqueleto da marca como partição da identidade (migration 031).

O que motivou está medido no banco vivo: o prompt "cardhu scott" produziu a
entidade `single malt Scotch whisky` com 24 mercados de vocabulário 100%
Cardhu, e resolver o mesmo nome genérico com `brand=Glenfiddich` devolveu
`method: alias` para ESSA entidade — pelo caminho O(1), o mais barato da
resolução. A segunda marca herdaria o vocabulário e a demanda da primeira.

Aqui se prende só o que o schema garante; quem passa a USAR a partição na
decisão de identidade é o passo seguinte (`catalog_service.resolve`).
"""

import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.brand import Brand
from app.db.models.product_catalog import ProductCatalog
from app.repositories.product_repository import ProductRepository
from app.services.catalog_service import CatalogService
from app.utils.text import normalize_alias


def _marca(nome: str) -> Brand:
    return Brand(
        brand_key=uuid.uuid4(),
        name=nome,
        name_normalized=normalize_alias(nome),
        first_seen_source="test",
    )


async def test_mesma_marca_em_grafias_diferentes_e_uma_so(db_session: AsyncSession) -> None:
    """Cardhu, CARDHU e cardhu são a mesma marca. Sem isso, o veto
    cruzado da demanda teria três listas para a mesma empresa."""
    db_session.add(_marca("Cardhu"))
    await db_session.commit()

    db_session.add(_marca("CARDHU"))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_marca_sem_forma_normalizada_e_recusada(db_session: AsyncSession) -> None:
    """Mesma guarda da migration 028: texto que normaliza para vazio (só
    pontuação, só emoji) vira um ímã que casa com qualquer consulta sem forma
    normalizada. Uma marca assim não pode entrar no cadastro."""
    db_session.add(Brand(brand_key=uuid.uuid4(), name="!!!", name_normalized="", first_seen_source="test"))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_entidade_de_marca_exige_familia_generica(db_session: AsyncSession) -> None:
    """Não existe marca sem produto genérico por trás — o LLM de identificação
    sempre devolve o nome genérico junto, porque o prompt manda pôr a marca só
    em `brand`. Uma entidade de marca órfã não teria contra o que comparar o
    share da categoria."""
    marca = _marca("Cardhu")
    db_session.add(marca)
    await db_session.commit()

    db_session.add(
        ProductCatalog(
            entity_key=uuid.uuid4(),
            canonical_title="Cardhu single malt Scotch whisky",
            brand_key=marca.brand_key,
            generic_entity_key=None,  # órfã
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_generica_e_marca_convivem_como_entidades_separadas(db_session: AsyncSession) -> None:
    """O par que o bug produzia como UMA entidade: a categoria e a marca
    coexistem, cada uma com seu vocabulário, ligadas por `generic_entity_key`.
    A genérica é a própria família, então seu `generic_entity_key` é NULL."""
    familia = ProductCatalog(entity_key=uuid.uuid4(), canonical_title="single malt Scotch whisky", category="Spirits")
    db_session.add(familia)
    await db_session.commit()

    marca = _marca("Cardhu")
    db_session.add(marca)
    await db_session.commit()

    filha = ProductCatalog(
        entity_key=uuid.uuid4(),
        canonical_title="Cardhu single malt Scotch whisky",
        category="Spirits",
        brand_key=marca.brand_key,
        generic_entity_key=familia.entity_key,
    )
    db_session.add(filha)
    await db_session.commit()

    assert familia.brand_key is None and familia.generic_entity_key is None
    assert filha.brand_key == marca.brand_key
    assert filha.generic_entity_key == familia.entity_key
    assert filha.entity_key != familia.entity_key


# ---------------------------------------------------------------------------
# Comportamento: a resolução particionada (catalog_service._resolver_particao)
# ---------------------------------------------------------------------------


def _sem_match() -> AsyncMock:
    """Passo 3 desligado: o que se mede aqui é a partição, não o LLM."""
    matcher = AsyncMock()
    matcher.pick.return_value = None
    return matcher


def _mercado(termo: str) -> dict:
    return {"USA": {"primary_name": termo, "search_terms": [termo], "excluded_terms": [], "confidence": 0.9}}


async def test_vocabulario_da_marca_nao_vai_para_o_produto(db_session: AsyncSession) -> None:
    """O estrago medido no relatório da Cardhu: a entidade chamada `single malt
    Scotch whisky` guardava 24 mercados de vocabulário 100% Cardhu — o rótulo
    dizia a categoria e o número media a marca.

    A proteção continua, em outro lugar. Desde 24/09/2026 o catálogo tem UM
    produto e a marca é vínculo, então os termos curados da marca vão para
    `brand_markets`, chaveados por (produto, marca, país), e o produto nasce
    limpo. "juan valdez café" e "café" são buscas diferentes no Google Ads e
    nunca podem dar o mesmo número.
    """
    sufixo = uuid.uuid4().hex[:8]
    service = CatalogService(db_session, matcher=_sem_match())
    repo = ProductRepository(db_session)
    nome = f"single malt scotch whisky {sufixo}"

    resolvida = await service.resolve(
        nome, source="test", brand=f"Cardhu{sufixo}", localized_markets=_mercado(f"cardhu {sufixo}")
    )
    produto = await repo.get_by_entity_key(resolvida.entity_key)
    marca = await repo.get_or_create_brand(name=f"Cardhu{sufixo}", source="test")

    assert not produto.localized_markets, "o produto não herda vocabulário de marca nenhuma"
    da_marca = await repo.markets_for(produto, marca.brand_key)
    assert da_marca, "os termos curados são da marca, e ficam com ela"
    assert await repo.markets_for(produto, None) == {}, "sem marca, o produto ainda não tem vocabulário"


async def test_duas_marcas_do_mesmo_produto_nao_se_misturam(db_session: AsyncSession) -> None:
    """A medição de 27/08/2026, agora com o modelo novo: `single malt Scotch
    whisky` + Cardhu e + Glenfiddich dividem o MESMO produto — é isso que o
    catálogo passou a exigir — e mesmo assim cada uma mede o próprio
    vocabulário. Sem isso, a segunda marca herdaria a demanda da primeira."""
    sufixo = uuid.uuid4().hex[:8]
    service = CatalogService(db_session, matcher=_sem_match())
    repo = ProductRepository(db_session)
    nome = f"single malt scotch whisky {sufixo}"

    uma = await service.resolve(
        nome, source="test", brand=f"Cardhu{sufixo}", localized_markets=_mercado(f"cardhu {sufixo}")
    )
    outra = await service.resolve(
        nome, source="test", brand=f"Glen{sufixo}", localized_markets=_mercado(f"glen {sufixo}")
    )

    assert uma.entity_key == outra.entity_key, "é o mesmo produto"
    produto = await repo.get_by_entity_key(uma.entity_key)
    cardhu = await repo.get_or_create_brand(name=f"Cardhu{sufixo}", source="test")
    glen = await repo.get_or_create_brand(name=f"Glen{sufixo}", source="test")
    termos_cardhu = await repo.markets_for(produto, cardhu.brand_key)
    termos_glen = await repo.markets_for(produto, glen.brand_key)

    assert termos_cardhu != termos_glen, "cada marca mede o que se busca por ela"
    assert termos_cardhu, termos_glen


async def test_segunda_resolucao_da_mesma_marca_sai_pelo_alias(db_session: AsyncSession) -> None:
    """A chave gravada é `marca + nome`, então a próxima consulta da mesma marca
    resolve no passo 1 — o caminho O(1) — em vez de refazer trigram e LLM."""
    sufixo = uuid.uuid4().hex[:8]
    service = CatalogService(db_session, matcher=_sem_match())
    nome = f"air fryer {sufixo}"

    primeira = await service.resolve(nome, source="test", brand=f"Philips{sufixo}")
    segunda = await service.resolve(nome, source="test", brand=f"Philips{sufixo}")

    assert primeira.method == "created"
    assert segunda.method == "alias"
    assert segunda.entity_key == primeira.entity_key


async def test_a_marca_resolve_para_o_mesmo_produto_do_nome_generico(db_session: AsyncSession) -> None:
    """A inversão que isto impede, medida em 24/09/2026: pedir `coffee` devolvia
    a entidade da marca e pedir `Juan Valdez coffee` devolvia a genérica, e cada
    acerto gravava um alias que tornava o erro permanente. O mesmo produto
    existia em dois lugares, e cada lugar comprava o próprio vocabulário na
    DataForSEO — BOL, BRA e COL foram coletados duas vezes."""
    sufixo = uuid.uuid4().hex[:8]
    service = CatalogService(db_session, matcher=_sem_match())
    nome = f"air fryer {sufixo}"

    generica = await service.resolve(nome, source="test")
    com_marca = await service.resolve(nome, source="test", brand=f"Philips{sufixo}")

    assert com_marca.entity_key == generica.entity_key, "um produto, com ou sem marca"
    assert com_marca.brand == f"Philips{sufixo}", "a marca vem ao lado, não dentro da identidade"


async def test_marca_sem_forma_normalizada_resolve_como_generico(db_session: AsyncSession) -> None:
    """Marca que normaliza para vazio (só pontuação/emoji) não pode ser chave de
    nada — mesma regra da migration 028. O produto resolve como genérico em vez
    de a resolução falhar."""
    sufixo = uuid.uuid4().hex[:8]
    service = CatalogService(db_session, matcher=_sem_match())
    repo = ProductRepository(db_session)

    resolvida = await service.resolve(f"air fryer {sufixo}", source="test", brand="!!!")
    entidade = await repo.get_by_entity_key(resolvida.entity_key)

    assert entidade.brand_key is None and entidade.generic_entity_key is None
    assert entidade.canonical_title == f"air fryer {sufixo}"


async def test_dois_produtos_da_mesma_marca_seguem_separados(db_session: AsyncSession) -> None:
    """O caso que a concatenação fundia: dentro da mesma marca, a medida volta a
    ser `air purifier` x `airfryer` = 0.278, abaixo do limiar. Sufixos
    diferentes de propósito — com o mesmo sufixo nos dois, ele viraria o token
    compartilhado e mascararia o que se mede."""
    service = CatalogService(db_session, matcher=_sem_match())
    marca = f"Philips{uuid.uuid4().hex[:6]}"

    fryer = await service.resolve(f"air fryer {uuid.uuid4().hex[:8]}", source="test", brand=marca)
    purificador = await service.resolve(f"air purifier {uuid.uuid4().hex[:8]}", source="test", brand=marca)

    assert purificador.entity_key != fryer.entity_key
    assert purificador.method == "created"


async def test_lista_de_outras_marcas_exclui_a_propria(db_session: AsyncSession) -> None:
    """A lista que alimenta o veto cruzado da demanda: todas as marcas do
    cadastro MENOS a do produto. Incluir a própria vetaria os termos que a
    entidade declara serem dela."""
    sufixo = uuid.uuid4().hex[:6]
    cardhu, glenfiddich = _marca(f"Cardhu{sufixo}"), _marca(f"Glenfiddich{sufixo}")
    db_session.add_all([cardhu, glenfiddich])
    await db_session.commit()

    repo = ProductRepository(db_session)
    outras = await repo.list_other_brand_names(cardhu.brand_key)
    assert glenfiddich.name_normalized in outras
    assert cardhu.name_normalized not in outras

    # Produto sem marca: lista vazia, o veto não age (a categoria soma as marcas).
    assert await repo.list_other_brand_names(None) == []


async def test_resposta_expoe_o_produto_e_a_marca_separados(db_session: AsyncSession) -> None:
    """O relatório precisa das duas partes — "Cardhu · single malt Scotch
    whisky" — e fatiar o título para obter isso seria frágil. Desde 24/09/2026 o
    título é o do PRODUTO e a marca vem ao lado: é ela que a análise grava como
    vínculo, e é o par que a narrativa final explica."""
    sufixo = uuid.uuid4().hex[:8]
    service = CatalogService(db_session, matcher=_sem_match())
    nome = f"single malt scotch whisky {sufixo}"

    com_marca = await service.resolve(nome, source="test", brand=f"Cardhu{sufixo}")
    assert com_marca.brand == f"Cardhu{sufixo}"
    assert com_marca.canonical_title == nome, "o título do catálogo é o do produto"

    generico = await service.resolve(f"air fryer {sufixo}", source="test")
    assert generico.brand is None


async def test_lista_de_todas_as_marcas_inclui_a_propria(db_session: AsyncSession) -> None:
    """A medição da CATEGORIA veta TODAS as marcas, inclusive a do produto: ela é
    o denominador que responde "o mercado em abstrato existe?", e só responde
    isso se for demanda sem marca nenhuma. Diferente do veto dos sinais da
    marca, que poupa a dela — ver list_other_brand_names."""
    sufixo = uuid.uuid4().hex[:6]
    cardhu, glenfiddich = _marca(f"Cardhu{sufixo}"), _marca(f"Glenfiddich{sufixo}")
    db_session.add_all([cardhu, glenfiddich])
    await db_session.commit()

    todas = await ProductRepository(db_session).list_all_brand_names()
    assert cardhu.name_normalized in todas and glenfiddich.name_normalized in todas
