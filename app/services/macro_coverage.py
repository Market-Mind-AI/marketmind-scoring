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

import asyncio
from dataclasses import dataclass, field
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


def fontes_sem_cobertura_de(country) -> frozenset[DataSource]:  # noqa: ANN001
    """As fontes que não cobrem este país, lidas do mapeamento dele.

    Cada coluna de identificação em `countries` é o código do país NAQUELA
    fonte. Sem código, a fonte não o conhece — e é isso que faz disto um dado
    verificável em vez de uma lista de exceções mantida à mão: o mapeamento já
    precisa existir para a coleta funcionar, e a ausência dele é a resposta.

    Medido no armazém do OECD em 2026-09-22: o índice de preços imobiliários
    (RHP) tem série para 22 dos 24 países suportados. Argentina e Peru não
    estão, e eram duas tarefas falhando todas as noites desde sempre.
    """
    sem: set[DataSource] = set()
    if getattr(country, "oecd_ref_area", None) is None:
        sem.add(DataSource.OECD)
    if getattr(country, "undata_location_id", None) is None:
        sem.add(DataSource.UNDATA)
    return frozenset(sem)


def macro_gaps(country_iso3: str, *, fontes_sem_cobertura: frozenset[DataSource] = frozenset()) -> list[CoverageGap]:
    """Os gaps de dado macro de um país: World Bank, OECD e UN Data.

    `fontes_sem_cobertura` tira do pedido as fontes que comprovadamente não têm
    este país — ver `fontes_sem_cobertura_de`, que responde isso a partir do
    mapeamento da tabela `countries` em vez de uma lista mantida à mão.

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
        # Uma fonte que não cobre este país não é perguntada. Pedir e receber
        # "não tenho" falha TODA noite, para sempre — e uma falha que nunca vai
        # deixar de acontecer treina quem lê o log a ignorar falhas.
        #
        # Não é o mesmo que a fonte estar fora do ar, e a diferença é a que o
        # sistema inteiro usa para decidir se insiste ou desiste.
        if fonte in fontes_sem_cobertura:
            continue
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


@dataclass
class Auditoria:
    """O retrato de frescor do macro, sem juízo de valor embutido: quem lê
    decide o que é aceitável. Ver `MacroPrewarmService.auditar`."""

    paises: int = 0
    indicadores: int = 0
    vencidos: int = 0
    paises_com_vencidos: list[str] = field(default_factory=list)


# Quantos países aquecer ao mesmo tempo.
#
# Era um de cada vez, e com 24 países isso não incomodava. Com 151 passou a
# doer: medido em 23/09/2026, ~7,5 min por país — 15 horas para encher os
# candidatos, contra uma janela noturna de uma hora.
#
# O custo é quase todo do UN Data, que só devolve UM ANO por chamada: 4
# indicadores × 11 anos são 44 requisições por país, em série. Por isso o limite
# é BAIXO e não generoso — em 25/08/2026 essa mesma API ficou 29 minutos fora e
# nove países saíram de um ranking. Multiplicar a carga por dez para ganhar
# tempo seria trocar espera por indisponibilidade.
_PAISES_EM_PARALELO = 4


class MacroPrewarmService:
    """Mantém o dado macro dos países suportados quente, fora de qualquer
    análise. Pensado para um cron — não há agendador em processo aqui."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._country_repo = CountryRepository(session)
        self._coverage_service = CoverageService(session)

    async def _vencidos_de(self, iso3: str) -> tuple[int, list[CoverageGap]]:
        """Quantos gaps macro este país tem e quais estão vencidos. LEITURA
        PURA — não coleta nada.

        Extraído para que `prewarm` e `auditar` façam exatamente a mesma
        pergunta. É a razão de `macro_gaps` existir num lugar só, pela mesma
        lição: duas construções separadas divergem, e a divergência aparece
        como um país que o pré-aquecimento achou quente e a análise achou frio.
        """
        pais = await self._country_repo.get_by_iso3(iso3)
        gaps = macro_gaps(iso3, fontes_sem_cobertura=fontes_sem_cobertura_de(pais))
        statuses = await self._coverage_service.check_coverage(gaps)
        return len(gaps), [gap for gap, status in zip(gaps, statuses, strict=True) if not status.covered]

    async def auditar(self) -> Auditoria:
        """O dado macro dos países OFERECIDOS está quente AGORA?

        Mesma pergunta do pré-aquecimento, sem a parte que coleta. Existe
        porque "o pré-aquecimento rodou" e "o dado está quente" são coisas
        diferentes, e só a segunda é o que o usuário recebe.

        Só os oferecidos, e isso é o ponto: um CANDIDATO com dado faltando não é
        um achado, é um candidato — estar incompleto é o estado normal dele até
        ser enchido. Medido em 23/09/2026, logo depois de carregar os 127: a
        sentinela virou vermelha relatando "1944 vencidos em 122 países" e teria
        mandado alarme sev1 todas as noites até o último encher. Uma falha que
        nunca deixa de acontecer ensina quem lê o alarme a ignorar alarmes, que
        é o oposto do que ele serve.

        Uma fonte fora do ar não vira "tudo vencido": `freshness` levanta, e
        quem chama decide. Não conseguir perguntar é um resultado diferente de
        perguntar e ouvir "não tenho".
        """
        auditoria = Auditoria()
        for country in await self._country_repo.list_all():
            quantos, vencidos = await self._vencidos_de(country.iso3)
            auditoria.paises += 1
            auditoria.indicadores += quantos
            if vencidos:
                auditoria.vencidos += len(vencidos)
                auditoria.paises_com_vencidos.append(country.iso3)
        return auditoria

    async def _aquecer_um(self, iso3: str, relatorio: dict[str, int]) -> None:
        """Uma passagem por um país, em SESSÃO PRÓPRIA.

        Sessão própria porque os países passaram a rodar em paralelo, e uma
        `AsyncSession` não é segura sob concorrência — duas tarefas na mesma
        sessão corrompem o estado uma da outra de um jeito que não aparece em
        teste pequeno.
        """
        from app.db.session import async_session_factory

        async with async_session_factory() as sessao:
            servico = MacroPrewarmService(sessao)
            quantos, faltando = await servico._vencidos_de(iso3)
            relatorio["paises"] += 1
            relatorio["ja_frescos"] += quantos - len(faltando)
            if faltando:
                _job_id, resultados = await servico._coverage_service.check_or_collect(
                    faltando, requested_by="macro_prewarm"
                )
                relatorio["coletados"] += sum(1 for r in resultados if r.status == JobTaskStatus.DONE)
                relatorio["falhados"] += sum(1 for r in resultados if r.status == JobTaskStatus.FAILED)
                # Pergunta de novo, agora que coletou: é a resposta depois da
                # coleta que diz se este país tem o dado, não a de antes.
                _quantos, faltando = await servico._vencidos_de(iso3)
            if not faltando and await servico._promover(iso3):
                relatorio["promovidos"] += 1
            await sessao.commit()

    async def prewarm(self, *, countries: list[str] | None = None) -> dict[str, int]:
        # Candidatos incluídos: é justamente o pré-aquecimento que lhes dá dado
        # para poderem ser medidos. Não são oferecidos por isso — quem oferece é
        # `list_all`, que só devolve os já medidos (ver a migration 042).
        alvos = countries or [c.iso3 for c in await self._country_repo.list_including_candidates()]
        relatorio = {"paises": 0, "ja_frescos": 0, "coletados": 0, "falhados": 0, "promovidos": 0}

        limite = asyncio.Semaphore(_PAISES_EM_PARALELO)

        async def _com_limite(iso3: str) -> None:
            async with limite:
                try:
                    await self._aquecer_um(iso3, relatorio)
                except Exception:  # noqa: BLE001 — um país que explode não leva os outros
                    logger.exception(f"Pré-aquecimento de {iso3} falhou")
                    relatorio["paises"] += 1
                    relatorio["falhados"] += 1

        await asyncio.gather(*(_com_limite(iso3) for iso3 in alvos))

        logger.info(f"Pré-aquecimento macro: {relatorio}")
        return relatorio

    async def _promover(self, iso3: str) -> bool:
        """Torna um candidato oferecível, quando o dado dele de fato chegou.

        É isto que faz "disponível" significar MEDIDO em vez de cadastrado. Um
        país entra na tabela como candidato (migration 043) e só aparece no
        seletor depois que uma passagem coletou tudo que ele precisa e não
        sobrou nada vencido.

        Importa porque a regra do grafo é que nada parcial é entregue: um país
        pedido que não pontua faz a análise INTEIRA desistir, levando junto os
        países bons escolhidos na mesma consulta. Oferecer antes de coletar é
        vender uma falha.

        Só promove — nunca rebaixa. Um país que já entregou análise não deixa de
        ser oferecido porque uma fonte ficou fora do ar esta noite; para isso
        existe a insistência, e tirá-lo do seletor por uma queda transitória
        seria trocar um problema pequeno por outro maior.
        """
        pais = await self._country_repo.get_by_iso3(iso3)
        if pais is None or pais.analysis_ready:
            return False
        pais.analysis_ready = True
        await self._session.flush()
        logger.info(f"País promovido a disponível: {iso3} — o macro dele está completo")
        return True
