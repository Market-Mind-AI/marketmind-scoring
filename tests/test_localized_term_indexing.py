"""Os `search_terms` de cada mercado precisam virar aliases, senão o
vocabulário multilíngue que o LLM de enrichment produz fica invisível para o
passo de trigram — que busca em `product_aliases`, não no JSONB.

Antes desta indexação, resolver "freidora de aire" criava uma entidade NOVA
mesmo com esse termo já gravado em localized_markets['ESP'] da air fryer.
"""

import uuid
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.product_repository import ProductRepository
from app.services.catalog_service import CatalogService


def _sem_match() -> AsyncMock:
    """Passo 3 desligado: estes testes exercitam alias e trigram, e uma
    chamada real de LLM tornaria a suíte não-determinística."""
    matcher = AsyncMock()
    matcher.pick.return_value = None
    return matcher


def _mercados(sufixo: str) -> dict:
    return {
        "ESP": {
            "primary_name": f"freidora de aire {sufixo}",
            "search_terms": [f"freidora de aire {sufixo}", f"freidora sin aceite {sufixo}"],
            "excluded_terms": [f"freidora de aceite {sufixo}"],
            "confidence": 0.95,
        },
        "BRA": {
            "primary_name": f"air fryer {sufixo}",
            "search_terms": [f"air fryer {sufixo}", f"fritadeira sem oleo {sufixo}"],
            "excluded_terms": [],
            "confidence": 0.95,
        },
    }


async def test_termos_localizados_viram_aliases(db_session: AsyncSession) -> None:
    sufixo = uuid.uuid4().hex[:8]
    service = CatalogService(db_session, matcher=_sem_match())

    criado = await service.resolve(
        f"air fryer {sufixo}", source="test", localized_markets=_mercados(sufixo)
    )
    assert criado.method == "created"

    repo = ProductRepository(db_session)
    donos = await repo.existing_alias_owners(
        {
            f"freidora de aire {sufixo}",
            f"freidora sin aceite {sufixo}",
            f"fritadeira sem oleo {sufixo}",
        }
    )
    assert set(donos) == {
        f"freidora de aire {sufixo}",
        f"freidora sin aceite {sufixo}",
        f"fritadeira sem oleo {sufixo}",
    }, "todo search_term deve ter virado alias da entidade"
    assert all(k == criado.entity_key for k in donos.values())


async def test_consulta_em_outro_idioma_reusa_a_entidade(db_session: AsyncSession) -> None:
    """A lacuna que isto fecha: consultar em espanhol um produto identificado
    em português tem que reaproveitar a entidade, não criar outra."""
    sufixo = uuid.uuid4().hex[:8]
    service = CatalogService(db_session, matcher=_sem_match())

    criado = await service.resolve(
        f"air fryer {sufixo}", source="test", localized_markets=_mercados(sufixo)
    )
    espanhol = await service.resolve(f"freidora de aire {sufixo}", source="test")

    assert espanhol.entity_key == criado.entity_key
    assert espanhol.method == "alias", "o termo indexado dá acerto exato, o passo mais barato"


async def test_excluded_terms_nao_sao_indexados(db_session: AsyncSession) -> None:
    """`freidora de aceite` é fritadeira de ÓLEO — o catálogo declara que
    significa outro produto. Indexá-lo faria a air fryer responder por ele."""
    sufixo = uuid.uuid4().hex[:8]
    service = CatalogService(db_session, matcher=_sem_match())
    await service.resolve(f"air fryer {sufixo}", source="test", localized_markets=_mercados(sufixo))

    repo = ProductRepository(db_session)
    donos = await repo.existing_alias_owners({f"freidora de aceite {sufixo}"})
    assert donos == {}, "excluded_terms nunca podem virar alias"


async def test_termo_de_outra_entidade_nao_e_roubado(db_session: AsyncSession) -> None:
    """`alias_normalized` é único global. Se um termo já aponta para outra
    entidade, o mapeamento existente prevalece — reapontar em silêncio trocaria
    a identidade de um produto já resolvido."""
    sufixo = uuid.uuid4().hex[:8]
    service = CatalogService(db_session, matcher=_sem_match())

    primeiro = await service.resolve(f"fritadeira sem oleo {sufixo}", source="test")
    assert primeiro.method == "created"

    # Outro produto reivindica o mesmo termo entre os seus search_terms.
    segundo = await service.resolve(
        f"outro produto {sufixo}", source="test", localized_markets=_mercados(sufixo)
    )
    assert segundo.entity_key != primeiro.entity_key

    repo = ProductRepository(db_session)
    donos = await repo.existing_alias_owners({f"fritadeira sem oleo {sufixo}"})
    assert donos[f"fritadeira sem oleo {sufixo}"] == primeiro.entity_key, "o dono original permanece"


async def test_termos_nao_latinos_sao_ignorados(db_session: AsyncSession) -> None:
    """normalize_alias remove tudo fora de [a-z0-9], então katakana e árabe
    normalizam para string vazia. Devem ser pulados sem quebrar a indexação
    — é limitação conhecida do casamento lexical, não bug."""
    sufixo = uuid.uuid4().hex[:8]
    service = CatalogService(db_session, matcher=_sem_match())

    resultado = await service.resolve(
        f"air fryer {sufixo}",
        source="test",
        localized_markets={
            "JPN": {
                "primary_name": "ノンフライヤー",
                "search_terms": ["ノンフライヤー", "エアフライヤー"],
                "excluded_terms": [],
                "confidence": 0.9,
            }
        },
    )
    assert resultado.method == "created", "termos sem forma latina não podem quebrar a resolução"
