"""O que um país precisa ter de dado macro, e como mantê-lo quente.

DUAS PERGUNTAS, UMA RESPOSTA SÓ. "Quais gaps macro este país precisa" é
respondido aqui e em nenhum outro lugar, porque quem coleta durante a análise
(`analysis_service._ensure_coverage`) e quem pré-aquece fora dela precisam
produzir exatamente a mesma lista. Duas construções separadas divergem — é a
mesma lição dos seeds de `keywords_for_keywords`, que só ficaram consistentes
quando passaram a ter um construtor único.

POR QUE PRÉ-AQUECER. Os indicadores macro são anuais, lidos como "último valor
conhecido" e válidos por 90 dias (`country_indicator_ttl_days`): não têm nada a
ver com o produto analisado nem com o instante da consulta. Mesmo assim, até
27/08/2026 eles eram coletados DENTRO da análise, o que amarrava o resultado do
usuário à disponibilidade da fonte naquele minuto. Em 25/08 a API do UN
Population Data Portal ficou 29 minutos fora e 9 dos 24 países saíram do
ranking por causa disso.

Com a regra "nada parcial sai" (D3) essa mesma queda deixaria de custar países e
passaria a custar TEMPO — a análise ficaria esperando. Pré-aquecer é o que torna
isso raro em vez de rotineiro: o dado já está no lugar quando alguém pede.

Não substitui o retry: a análise continua sabendo se virar sozinha se algo
estiver faltando. Reduz a frequência com que precisa.
"""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import logger
from app.domain.enums import DataSource, JobTaskStatus
from app.repositories.country_repository import CountryRepository
from app.schemas.coverage import CoverageGap
from app.services.coverage_service import CoverageService
from app.services.indicators.oecd import RAW_INDICATOR_CODES as OECD_RAW_CODES
from app.services.indicators.undata import RAW_INDICATOR_CODES as UNDATA_RAW_CODES
from app.services.indicators.worldbank import RAW_INDICATOR_CODES as WORLDBANK_RAW_CODES
from app.utils.period import COUNTRY_LEVEL_PERIOD_KEY

# Quanto de histórico pedir às fontes anuais/trimestrais. Generoso de propósito,
# porque fórmulas como GDP Growth e Real Estate Growth precisam de vários anos.
# Não confundir com frescor, que é `country_indicator_ttl_days`: este é "até
# onde buscar", aquele é "quando buscar de novo".
_ANNUAL_LOOKBACK_YEARS = 10


def macro_gaps(country_iso3: str) -> list[CoverageGap]:
    """Os gaps de dado macro de um país: World Bank, OECD e UN Data.

    Uma linha de cobertura por (país, indicador), não uma por mês. O laço sobre
    os últimos 6 meses que existia aqui produzia 6 × 12 gaps do World Bank por
    país e, como a busca de cobertura casa `period_key` exato, a primeira
    análise de cada mês não achava linha e recoletava tudo — o TTL nunca era
    consultado. Foi a razão de uma análise de dois países ter levado 61 minutos,
    91% deles rebaixando números idênticos aos já guardados.
    """
    ano = datetime.now(UTC).year
    params = {"start_year": ano - _ANNUAL_LOOKBACK_YEARS, "end_year": ano}
    gaps: list[CoverageGap] = []
    for fonte, codigos, extra in (
        (DataSource.WORLDBANK, WORLDBANK_RAW_CODES, params),
        (DataSource.OECD, OECD_RAW_CODES, {"start_period": str(ano - _ANNUAL_LOOKBACK_YEARS), "end_period": str(ano)}),
        (DataSource.UNDATA, UNDATA_RAW_CODES, params),
    ):
        for lista in codigos.values():
            for code in lista:
                gaps.append(
                    CoverageGap(
                        source=fonte,
                        entity_key=None,
                        country=country_iso3,
                        indicator_code=code,
                        period_key=COUNTRY_LEVEL_PERIOD_KEY,
                        ttl_days=settings.country_indicator_ttl_days,
                        collection_params=extra,
                    )
                )
    return gaps


class MacroPrewarmService:
    """Mantém o dado macro dos países suportados quente, fora de qualquer
    análise. Pensado para um cron — não há agendador em processo aqui."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._country_repo = CountryRepository(session)
        self._coverage_service = CoverageService(session)

    async def prewarm(self, *, countries: list[str] | None = None) -> dict[str, int]:
        alvos = countries or [c.iso3 for c in await self._country_repo.list_all()]
        relatorio = {"paises": 0, "ja_frescos": 0, "coletados": 0, "falhados": 0}

        for iso3 in alvos:
            gaps = macro_gaps(iso3)
            statuses = await self._coverage_service.check_coverage(gaps)
            faltando = [gap for gap, status in zip(gaps, statuses, strict=True) if not status.covered]
            relatorio["paises"] += 1
            relatorio["ja_frescos"] += len(gaps) - len(faltando)
            if not faltando:
                continue
            _job_id, resultados = await self._coverage_service.check_or_collect(faltando, requested_by="macro_prewarm")
            relatorio["coletados"] += sum(1 for r in resultados if r.status == JobTaskStatus.DONE)
            relatorio["falhados"] += sum(1 for r in resultados if r.status == JobTaskStatus.FAILED)

        logger.info(f"Pré-aquecimento macro: {relatorio}")
        return relatorio
