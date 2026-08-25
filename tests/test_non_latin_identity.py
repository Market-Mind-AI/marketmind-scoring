"""Escrita não-latina não pode colapsar produtos distintos numa entidade.

`normalize_alias` removia tudo fora de [a-z0-9], então um texto inteiramente em
japonês, chinês, coreano, russo ou árabe normalizava para string VAZIA — e
`resolve()` gravava esse vazio como alias da entidade criada. A partir daí,
qualquer outro texto não-latino casava com ele no PASSO 1, o caminho O(1), onde
nem o trigram nem o veto por evidência negativa chegam a rodar.

Reproduzido antes da correção:

    ノンフライヤー   (air fryer, japonês)  -> created  entity=161b52ba
    空气炸锅        (air fryer, chinês)   -> alias    entity=161b52ba
    Пылесос-робот  (aspirador, russo)    -> alias    entity=161b52ba

Um aspirador russo virava uma air fryer japonesa. É a mesma fusão silenciosa
que o veto existe para impedir, entrando por uma porta que o veto não guarda.
"""

import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.product_alias import ProductAlias
from app.repositories.product_repository import ProductRepository
from app.services.catalog_service import CatalogService
from app.utils.text import normalize_alias


def _sem_match() -> AsyncMock:
    matcher = AsyncMock()
    matcher.pick.return_value = None
    return matcher


async def test_escritas_nao_latinas_nao_colapsam_numa_entidade(db_session: AsyncSession) -> None:
    sufixo = uuid.uuid4().hex[:6]
    service = CatalogService(db_session, matcher=_sem_match())

    japones = await service.resolve(f"ノンフライヤー{sufixo}", source="test")
    chines = await service.resolve(f"空气炸锅{sufixo}", source="test")
    russo = await service.resolve(f"Пылесос-робот{sufixo}", source="test")

    chaves = {japones.entity_key, chines.entity_key, russo.entity_key}
    assert len(chaves) == 3, "três produtos diferentes, três entidades"
    assert [r.method for r in (japones, chines, russo)] == ["created"] * 3


async def test_termo_nao_latino_vira_alias_e_reresolve_pelo_passo_1(db_session: AsyncSession) -> None:
    """O outro lado da mesma moeda: preservar a escrita não serve só para
    separar, serve para RECUPERAR. Antes, nem o japonês casava consigo mesmo."""
    sufixo = uuid.uuid4().hex[:6]
    service = CatalogService(db_session, matcher=_sem_match())

    criado = await service.resolve(f"ノンフライヤー{sufixo}", source="test")
    de_novo = await service.resolve(f"ノンフライヤー{sufixo}", source="test")

    assert de_novo.entity_key == criado.entity_key
    assert de_novo.method == "alias"


async def test_termos_localizados_nao_latinos_sao_indexados(db_session: AsyncSession) -> None:
    """46 termos curados do catálogo real, em 5 mercados (JPN, CHN, KOR, RUS,
    SAU), não tinham forma normalizada e nunca viravam alias."""
    sufixo = uuid.uuid4().hex[:6]
    service = CatalogService(db_session, matcher=_sem_match())
    criado = await service.resolve(
        f"air fryer {sufixo}",
        source="test",
        localized_markets={
            "JPN": {
                "primary_name": f"ノンフライヤー{sufixo}",
                "search_terms": [f"ノンフライヤー{sufixo}", f"エアフライヤー{sufixo}"],
                "excluded_terms": [],
                "confidence": 0.9,
            }
        },
    )

    japones = await service.resolve(f"エアフライヤー{sufixo}", source="test")
    assert japones.entity_key == criado.entity_key
    assert japones.method == "alias"


async def test_texto_sem_forma_normalizada_nao_grava_alias(db_session: AsyncSession) -> None:
    """Pontuação e emoji continuam normalizando para vazio. A entidade é criada
    (o texto original ainda identifica algo para o usuário), mas nenhum alias é
    gravado — é o alias vazio que fazia o ímã."""
    service = CatalogService(db_session, matcher=_sem_match())

    primeiro = await service.resolve("!!!", source="test")
    segundo = await service.resolve("😀", source="test")

    assert primeiro.entity_key != segundo.entity_key
    vazios = await db_session.execute(select(ProductAlias).where(ProductAlias.alias_normalized == ""))
    assert vazios.scalars().all() == []


async def test_banco_recusa_alias_vazio(db_session: AsyncSession) -> None:
    """Terceira linha de defesa: mesmo que um chamador futuro contorne o
    serviço, a CHECK da migration 028 impede o estado corrompido."""
    repo = ProductRepository(db_session)
    entidade = await repo.create_entity(canonical_title=f"Produto {uuid.uuid4()}")
    await db_session.flush()

    with pytest.raises(Exception):  # IntegrityError da CHECK
        await repo.create_alias(alias_normalized="", entity_key=entidade.entity_key, source="test")
    await db_session.rollback()


async def test_backfill_poda_alias_derivado_obsoleto(db_session: AsyncSession) -> None:
    """O normalizador antigo TRUNCAVA termo de escrita mista: `Ninja エアフライヤー`
    virava o alias `ninja`. Dez marcas isoladas ficaram assim apontando para a
    air fryer — e marca isolada casa por igualdade exata, no passo 1, onde o
    veto não roda. O backfill reconcilia: o que a curadoria atual não produz
    mais é apagado."""
    sufixo = uuid.uuid4().hex[:6]
    repo = ProductRepository(db_session)
    entidade = await repo.create_entity(
        canonical_title=f"air fryer {sufixo}",
        localized_markets={
            "JPN": {
                "primary_name": f"Ninja エアフライヤー{sufixo}",
                "search_terms": [f"Ninja エアフライヤー{sufixo}"],
                "excluded_terms": [],
                "confidence": 0.9,
            }
        },
    )
    # O que a normalização antiga teria gravado.
    await repo.create_alias(
        alias_normalized=f"ninja{sufixo}", entity_key=entidade.entity_key, source="localized_market"
    )
    # E um alias de consulta real, que NUNCA pode ser podado.
    await repo.create_alias(
        alias_normalized=f"consulta real {sufixo}", entity_key=entidade.entity_key, source="analyze"
    )
    await db_session.commit()

    await CatalogService(db_session, matcher=_sem_match()).backfill_terms()

    donos = await repo.existing_alias_owners(
        {f"ninja{sufixo}", f"consulta real {sufixo}", normalize_alias(f"Ninja エアフライヤー{sufixo}")}
    )
    assert f"ninja{sufixo}" not in donos, "alias derivado truncado tem de ser podado"
    assert (
        donos.get(f"consulta real {sufixo}") == entidade.entity_key
    ), "alias de consulta real é evidência, nunca podado"
    assert donos.get(normalize_alias(f"Ninja エアフライヤー{sufixo}")) == entidade.entity_key
