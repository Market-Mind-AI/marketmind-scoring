"""Pré-aquecimento do dado macro, e a fonte única dos gaps.

Os indicadores macro são anuais, lidos como último valor conhecido e válidos
por 90 dias — não têm relação com o produto nem com o instante da consulta. E
mesmo assim eram coletados DENTRO da análise, o que amarrava o resultado do
usuário à fonte estar de pé naquele minuto: em 25/08/2026 a API do UN Data
ficou 29 minutos fora e 9 de 24 países saíram do ranking.
"""

import uuid
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import DataSource, JobTaskStatus
from app.schemas.coverage import GapStatus, JobTaskResult
from app.services.macro_coverage import MacroPrewarmService, macro_gaps
from app.utils.period import COUNTRY_LEVEL_PERIOD_KEY


def test_gaps_cobrem_as_tres_fontes_com_uma_linha_por_indicador() -> None:
    """Uma linha de cobertura por (país, indicador), não uma por mês: o laço de
    6 meses que existia produzia 6 × 12 gaps do World Bank por país e, como a
    busca casa `period_key` exato, a primeira análise de cada mês recoletava
    tudo. Foi a razão de uma análise de dois países levar 61 minutos."""
    gaps = macro_gaps("BRA")

    assert {g.source for g in gaps} == {DataSource.WORLDBANK, DataSource.OECD, DataSource.UNDATA}
    assert all(g.period_key == COUNTRY_LEVEL_PERIOD_KEY for g in gaps)
    assert all(g.entity_key is None for g in gaps), "dado de país não depende de produto"
    assert all(g.ttl_days == 90 for g in gaps)
    chaves = [(g.source, g.indicator_code) for g in gaps]
    assert len(chaves) == len(set(chaves)), "um gap por indicador"


def test_analise_e_preaquecimento_pedem_exatamente_a_mesma_coisa() -> None:
    """Duas construções separadas divergem, e aí o pré-aquecimento esquenta uma
    coisa e a análise pede outra. Mesma lição dos seeds do keywords_for_keywords."""
    from app.services import analysis_service

    assert analysis_service.macro_gaps is macro_gaps


async def test_prewarm_so_coleta_o_que_venceu(db_session: AsyncSession) -> None:
    """O TTL de 90 dias é o ponto: aquecer não pode virar recoleta mensal do
    catálogo inteiro, que é exatamente o custo que o TTL existe para evitar."""
    service = MacroPrewarmService(db_session)
    gaps_brl = macro_gaps("BRA")
    # Metade fresca, metade vencida.
    frescos = [
        GapStatus(
            source=g.source,
            entity_key=None,
            country=g.country,
            indicator_code=g.indicator_code,
            period_key=g.period_key,
            covered=i % 2 == 0,
        )
        for i, g in enumerate(gaps_brl)
    ]
    coletou = AsyncMock(
        return_value=(
            uuid.uuid4(),
            [
                JobTaskResult(
                    id=uuid.uuid4(),
                    source=DataSource.UNDATA,
                    entity_key=None,
                    country="BRA",
                    indicator_code="POPULATION_GROWTH",
                    period_key=COUNTRY_LEVEL_PERIOD_KEY,
                    status=JobTaskStatus.DONE,
                    ingestion_run_id=None,
                    error_message=None,
                )
            ],
        )
    )
    with (
        patch.object(service._coverage_service, "check_coverage", AsyncMock(return_value=frescos)),
        patch.object(service._coverage_service, "check_or_collect", coletou),
    ):
        relatorio = await service.prewarm(countries=["BRA"])

    assert relatorio["paises"] == 1
    assert relatorio["ja_frescos"] == len([f for f in frescos if f.covered])
    assert relatorio["coletados"] == 1
    despachados = coletou.await_args.args[0]
    assert len(despachados) == len([f for f in frescos if not f.covered]), "só o vencido é despachado"


async def test_prewarm_nao_despacha_nada_quando_tudo_esta_fresco(db_session: AsyncSession) -> None:
    service = MacroPrewarmService(db_session)
    todos_frescos = [
        GapStatus(
            source=g.source,
            entity_key=None,
            country=g.country,
            indicator_code=g.indicator_code,
            period_key=g.period_key,
            covered=True,
        )
        for g in macro_gaps("BRA")
    ]
    coletou = AsyncMock()
    with (
        patch.object(service._coverage_service, "check_coverage", AsyncMock(return_value=todos_frescos)),
        patch.object(service._coverage_service, "check_or_collect", coletou),
    ):
        relatorio = await service.prewarm(countries=["BRA"])

    coletou.assert_not_awaited()
    assert relatorio["coletados"] == 0 and relatorio["falhados"] == 0
