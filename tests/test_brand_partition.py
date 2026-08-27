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

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.brand import Brand
from app.db.models.product_catalog import ProductCatalog
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
