"""Estar no catálogo não é estar disponível para análise.

Eram a mesma coisa enquanto a tabela tinha só os 24 países que já funcionavam.
Deixam de ser no momento em que se quer acrescentar candidatos, e a diferença é
cara: a regra do grafo é que NADA PARCIAL É ENTREGUE, então um país oferecido
que não pontua faz a análise inteira desistir — levando junto os países bons
escolhidos na mesma consulta.

Medido em 23/09/2026 contra a API pública do World Bank, país por país: dos 217
do catálogo, 159 têm os 12 indicadores. A Nigéria não tem 5. "A API lista o
país" nunca foi "a API tem o dado dele".
"""

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.country import Country
from app.repositories.country_repository import CountryRepository


def test_the_route_serves_the_available_basket_and_not_the_catalogue(client: TestClient) -> None:
    """A rota tem de ler `list_all` (os medidos), nunca
    `list_including_candidates`. Afirmado sobre qual método é chamado porque o
    `TestClient` tem sessão própria: inserir uma linha no teste e não vê-la na
    resposta passaria de graça, medindo o isolamento e não a regra."""
    from unittest.mock import AsyncMock, patch

    disponivel = Country(iso3="ZZD", iso2="ZD", name="Medidolândia", continent="Africa", analysis_ready=True)

    with (
        patch.object(CountryRepository, "list_all", AsyncMock(return_value=[disponivel])) as medidos,
        patch.object(CountryRepository, "list_including_candidates", AsyncMock()) as candidatos,
    ):
        corpo = client.get("/countries").json()

    assert [c["iso3"] for c in corpo] == ["ZZD"]
    medidos.assert_awaited_once()
    candidatos.assert_not_awaited(), "oferecer candidato é vender uma falha"


async def test_the_weight_calibration_population_never_sees_candidates(db_session: AsyncSession) -> None:
    """O teste que mais importa, e o erro que ele impede seria invisível.

    Os pesos do Cap. 5 saem da população de referência, que é `list_all`.
    Incluir candidatos ali mudaria os pesos e, com eles, TODO MOS já calculado —
    uma mudança de MÉTODO entrando como efeito colateral de um cadastro, sem
    nada acusar."""
    db_session.add(Country(iso3="ZZB", iso2="ZB", name="Candidatolândia B", continent="Africa", analysis_ready=False))
    await db_session.flush()

    populacao = {c.iso3 for c in await CountryRepository(db_session).list_all()}

    assert "ZZB" not in populacao


async def test_the_prewarm_population_does_see_candidates(db_session: AsyncSession) -> None:
    """O outro lado: um candidato não pode ser medido sem que suas séries tenham
    sido coletadas alguma vez, e é o pré-aquecimento que as coleta."""
    db_session.add(Country(iso3="ZZC", iso2="ZC", name="Candidatolândia C", continent="Africa", analysis_ready=False))
    await db_session.flush()

    todos = {c.iso3 for c in await CountryRepository(db_session).list_including_candidates()}

    assert "ZZC" in todos


# --- a promoção: "disponível" quer dizer medido ------------------------------


async def test_a_candidate_whose_data_arrived_becomes_available(db_session: AsyncSession) -> None:
    """É isto que faz o seletor significar algo. O país entra como candidato
    (migration 043) e só é oferecido depois que uma passagem coletou tudo de que
    ele precisa e não sobrou nada vencido."""
    from unittest.mock import AsyncMock, patch

    from app.services.macro_coverage import MacroPrewarmService

    db_session.add(Country(iso3="ZZE", iso2="ZE", name="Promovilândia", continent="Africa", analysis_ready=False))
    await db_session.flush()

    servico = MacroPrewarmService(db_session)
    with patch.object(MacroPrewarmService, "_vencidos_de", AsyncMock(return_value=(17, []))):
        relatorio = await servico.prewarm(countries=["ZZE"])

    assert relatorio["promovidos"] == 1
    assert (await CountryRepository(db_session).get_by_iso3("ZZE")).analysis_ready is True


async def test_a_candidate_still_missing_data_is_not_promoted(db_session: AsyncSession) -> None:
    """Um país com indicador faltando continua fora do seletor — oferecê-lo
    faria a análise inteira desistir, inclusive os países bons pedidos junto."""
    from unittest.mock import AsyncMock, patch

    from app.schemas.coverage import CoverageGap
    from app.services.macro_coverage import MacroPrewarmService

    db_session.add(Country(iso3="ZZF", iso2="ZF", name="Incompletolândia", continent="Africa", analysis_ready=False))
    await db_session.flush()

    buraco = CoverageGap(
        source="worldbank", entity_key=None, country="ZZF", indicator_code="SP.POP.TOTL", period_key="COUNTRY"
    )
    servico = MacroPrewarmService(db_session)
    with (
        patch.object(MacroPrewarmService, "_vencidos_de", AsyncMock(return_value=(17, [buraco]))),
        patch.object(servico._coverage_service, "check_or_collect", AsyncMock(return_value=(None, []))),
    ):
        relatorio = await servico.prewarm(countries=["ZZF"])

    assert relatorio["promovidos"] == 0
    assert (await CountryRepository(db_session).get_by_iso3("ZZF")).analysis_ready is False


async def test_an_available_country_is_never_demoted(db_session: AsyncSession) -> None:
    """Só promove. Um país que já entregou análise não sai do seletor porque uma
    fonte ficou fora do ar esta noite — para isso existe a insistência, e
    rebaixá-lo por uma queda transitória troca um problema pequeno por um maior.
    """
    from unittest.mock import AsyncMock, patch

    from app.schemas.coverage import CoverageGap
    from app.services.macro_coverage import MacroPrewarmService

    db_session.add(Country(iso3="ZZG", iso2="ZG", name="Veteranolândia", continent="Africa", analysis_ready=True))
    await db_session.flush()

    buraco = CoverageGap(
        source="worldbank", entity_key=None, country="ZZG", indicator_code="SP.POP.TOTL", period_key="COUNTRY"
    )
    servico = MacroPrewarmService(db_session)
    with (
        patch.object(MacroPrewarmService, "_vencidos_de", AsyncMock(return_value=(17, [buraco]))),
        patch.object(servico._coverage_service, "check_or_collect", AsyncMock(return_value=(None, []))),
    ):
        await servico.prewarm(countries=["ZZG"])

    assert (await CountryRepository(db_session).get_by_iso3("ZZG")).analysis_ready is True
