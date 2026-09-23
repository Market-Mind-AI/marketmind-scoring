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
