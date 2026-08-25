"""O catálogo curava evidência negativa desde a Fase 5 (`excluded_terms`) e não
a usava para decidir identidade — só para filtrar volume de busca e para não
virar alias. Medido contra o banco real em 2026-08-25, com os termos localizados
indexados e o limiar 0.4 configurado: 44 dos 102 termos que o próprio catálogo
declara como "isto NÃO é este produto" resolviam para a entidade que os exclui.

    0.765  'öl fritteuse'        -> air fryer  via 'fritteuse ohne öl'
    0.727  'fritadeira a óleo'   -> air fryer  via 'fritadeira sem óleo'
    0.696  'fritadeira de óleo'  -> air fryer  via 'fritadeira sem óleo'

Fritadeira a ar e fritadeira a óleo são produtos opostos, e é exatamente por
serem opostos que nenhuma medida de semelhança os separa: opostos compartilham
toda a frase menos a palavra que decide. Esta suíte é o conjunto NEGATIVO que
faltava — os 89 termos medidos em 14/08 eram todos positivos, então a "precisão
de 100%" media só metade do problema.
"""

import uuid
from unittest.mock import AsyncMock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.product_excluded_term import ProductExcludedTerm
from app.repositories.product_repository import ProductRepository
from app.services.catalog_service import CatalogService


def _sem_match() -> AsyncMock:
    """Passo 3 desligado: o que se mede aqui é o passo 2. Com o LLM ligado, um
    veto correto ficaria indistinguível de um acerto do LLM."""
    matcher = AsyncMock()
    matcher.pick.return_value = None
    return matcher


def _mercados(sufixo: str) -> dict:
    """Recorte do dado real da air fryer (27 mercados no catálogo vivo),
    preservando os pares que colidem por trigram em cada idioma."""
    return {
        "BRA": {
            "primary_name": f"air fryer {sufixo}",
            "search_terms": [f"air fryer {sufixo}", f"fritadeira sem óleo {sufixo}"],
            "excluded_terms": [f"fritadeira de óleo {sufixo}", f"fritadeira a óleo {sufixo}"],
            "confidence": 0.95,
        },
        "ESP": {
            "primary_name": f"freidora de aire {sufixo}",
            "search_terms": [f"freidora de aire {sufixo}", f"freidora sin aceite {sufixo}"],
            "excluded_terms": [f"freidora de aceite {sufixo}"],
            "confidence": 0.95,
        },
    }


async def _excluidos(session: AsyncSession, entity_key: uuid.UUID) -> set[str]:
    result = await session.execute(
        select(ProductExcludedTerm.term_normalized).where(ProductExcludedTerm.entity_key == entity_key)
    )
    return set(result.scalars())


async def test_excluded_terms_viram_evidencia_negativa(db_session: AsyncSession) -> None:
    sufixo = uuid.uuid4().hex[:8]
    service = CatalogService(db_session, matcher=_sem_match())
    criado = await service.resolve(f"air fryer {sufixo}", source="test", localized_markets=_mercados(sufixo))

    guardados = await _excluidos(db_session, criado.entity_key)
    assert f"fritadeira de oleo {sufixo}" in guardados
    assert f"freidora de aceite {sufixo}" in guardados

    repo = ProductRepository(db_session)
    donos = await repo.existing_alias_owners({f"fritadeira de oleo {sufixo}"})
    assert donos == {}, "termo excluído nunca pode ser porta de entrada"


async def test_fritadeira_a_oleo_nao_funde_na_air_fryer(db_session: AsyncSession) -> None:
    """A regressão que motivou tudo. Sem o veto, `fritadeira de óleo` casa com o
    alias `fritadeira sem óleo` a 0.696 — acima do limiar 0.4 — e vira alias
    exato PERMANENTE da air fryer, misturando localized_markets e demanda dos
    dois produtos sem nenhum passo posterior perceber."""
    sufixo = uuid.uuid4().hex[:8]
    service = CatalogService(db_session, matcher=_sem_match())
    air_fryer = await service.resolve(f"air fryer {sufixo}", source="test", localized_markets=_mercados(sufixo))

    oleo = await service.resolve(f"fritadeira de óleo {sufixo}", source="test")

    assert oleo.entity_key != air_fryer.entity_key
    assert (
        oleo.method == "created"
    ), "o erro barato (entidade duplicada) é preferido de propósito ao caro (fusão silenciosa)"


async def test_o_veto_e_o_que_impede_a_fusao(db_session: AsyncSession) -> None:
    """Prova que a semelhança positiva SOZINHA aceitaria: a entidade aparece na
    lista de vetadas, e não como match. Sem isto, o teste acima passaria também
    num mundo onde o trigram simplesmente não alcançasse o termo."""
    sufixo = uuid.uuid4().hex[:8]
    service = CatalogService(db_session, matcher=_sem_match())
    air_fryer = await service.resolve(f"air fryer {sufixo}", source="test", localized_markets=_mercados(sufixo))

    repo = ProductRepository(db_session)
    resultado = await repo.trigram_search(f"fritadeira de oleo {sufixo}")

    assert resultado.match is None
    assert [p.entity_key for p in resultado.vetoed] == [air_fryer.entity_key]


async def test_entidade_vetada_vai_para_o_llm(db_session: AsyncSession) -> None:
    """Vetar não é decidir. O caso vai ao passo 3 COM a entidade vetada na
    lista — é ela que o LLM precisa julgar, e o teto de candidatos ou uma
    categoria divergente não podem escondê-la."""
    sufixo = uuid.uuid4().hex[:8]
    matcher = _sem_match()
    service = CatalogService(db_session, matcher=matcher)
    air_fryer = await service.resolve(f"air fryer {sufixo}", source="test", localized_markets=_mercados(sufixo))
    # A criação da air fryer também passa pelo passo 3 (catálogo vazio, nada a
    # casar). O que se mede aqui é a chamada da consulta vetada.
    matcher.pick.reset_mock()

    await service.resolve(f"fritadeira de óleo {sufixo}", source="test")

    matcher.pick.assert_awaited_once()
    candidatos = matcher.pick.await_args.kwargs["candidates"]
    assert air_fryer.entity_key in [c.entity_key for c in candidatos]


async def test_termo_excluido_ainda_resolve_para_a_dona_legitima(db_session: AsyncSession) -> None:
    """O veto é por entidade, não global. `panela de pressão elétrica` é
    excluída pela air fryer E é o produto legítimo de outra entidade — as duas
    coisas são verdade, e vetar a air fryer não pode cegar a dona certa."""
    sufixo = uuid.uuid4().hex[:8]
    service = CatalogService(db_session, matcher=_sem_match())
    mercados = _mercados(sufixo)
    mercados["BRA"]["excluded_terms"].append(f"panela de pressão elétrica {sufixo}")
    await service.resolve(f"air fryer {sufixo}", source="test", localized_markets=mercados)
    panela = await service.resolve(
        f"panela de pressão elétrica {sufixo}",
        source="test",
        localized_markets={
            "BRA": {
                "primary_name": f"panela de pressão elétrica {sufixo}",
                "search_terms": [f"panela de pressão elétrica {sufixo}"],
                "excluded_terms": [],
                "confidence": 0.9,
            }
        },
    )

    encontrada = await service.resolve(f"panela de pressão elétrica {sufixo}", source="test")
    assert encontrada.entity_key == panela.entity_key
    assert encontrada.method == "alias"


async def test_contradicao_no_dado_mantem_o_positivo(db_session: AsyncSession) -> None:
    """Achado real do catálogo vivo: `fritadeira elétrica` é search_term no BRA
    e excluded_term em PRT. Num empate desses o positivo vence — ele é evidência
    de que o termo RECUPERA o produto — e a contradição vai para o log em vez de
    virar um veto que derruba o próprio produto."""
    sufixo = uuid.uuid4().hex[:8]
    mercados = _mercados(sufixo)
    mercados["BRA"]["search_terms"].append(f"fritadeira elétrica {sufixo}")
    mercados["ESP"]["excluded_terms"].append(f"fritadeira elétrica {sufixo}")

    service = CatalogService(db_session, matcher=_sem_match())
    criado = await service.resolve(f"air fryer {sufixo}", source="test", localized_markets=mercados)

    guardados = await _excluidos(db_session, criado.entity_key)
    assert f"fritadeira eletrica {sufixo}" not in guardados
    reencontrado = await service.resolve(f"fritadeira elétrica {sufixo}", source="test")
    assert reencontrado.entity_key == criado.entity_key


async def test_backfill_indexa_os_dois_lados(db_session: AsyncSession) -> None:
    """Entidade criada antes destas features não tem nem alias localizado nem
    evidência negativa. O backfill preenche os dois na mesma passada — só os
    positivos ligaria o alcance multilíngue junto com as fusões que a evidência
    negativa existe para impedir."""
    sufixo = uuid.uuid4().hex[:8]
    repo = ProductRepository(db_session)
    entidade = await repo.create_entity(
        canonical_title=f"air fryer {sufixo}",
        category="Kitchen Appliances",
        localized_markets=_mercados(sufixo),
    )
    await db_session.commit()

    relatorio = await CatalogService(db_session, matcher=_sem_match()).backfill_terms()

    assert relatorio["aliases"] >= 4
    assert relatorio["excluidos"] >= 3
    guardados = await _excluidos(db_session, entidade.entity_key)
    assert f"fritadeira de oleo {sufixo}" in guardados
    donos = await repo.existing_alias_owners({f"freidora sin aceite {sufixo}"})
    assert donos.get(f"freidora sin aceite {sufixo}") == entidade.entity_key
