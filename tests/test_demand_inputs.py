"""Os insumos de demanda de um país saem de um lugar só.

A nota, a coleta, a régua e os candidatos montavam cada um o seu conjunto. A
régua lia sem os termos da categoria nas sementes, sem vetos e sem o veto de
outras marcas, e os candidatos eram pontuados sem vetos: a régua media uma
coisa e a nota outra.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.services.demand_inputs import demand_inputs
from app.services.indicators.pytrends import build_seed_terms

_MARCA = uuid.uuid4()


def _repo() -> SimpleNamespace:
    da_marca = {"ESP": {"search_terms": ["cardhu 12"], "excluded_terms": ["cardhu gin"], "search_language": "es"}}
    do_produto = {"ESP": {"search_terms": ["whisky de malta"]}}
    return SimpleNamespace(
        markets_for=AsyncMock(side_effect=lambda _e, brand_key: da_marca if brand_key else do_produto),
        list_other_brand_names=AsyncMock(side_effect=lambda brand_key: ["glenfiddich"] if brand_key else []),
        get_by_entity_key=AsyncMock(return_value=None),
    )


def _pais(iso3: str = "ESP") -> SimpleNamespace:
    return SimpleNamespace(iso3=iso3, iso2=iso3[:2], pytrends_geo=None, search_language=None)


_ENTIDADE = SimpleNamespace(entity_key=uuid.uuid4(), brand_key=None, generic_entity_key=None)


async def test_a_brand_reads_its_terms_its_category_its_vetoes_and_its_rivals() -> None:
    insumos = await demand_inputs(_repo(), _ENTIDADE, _pais(), brand_key=_MARCA)

    assert insumos.search_terms == ["cardhu 12"]
    assert insumos.category_terms == ["whisky de malta"]
    assert insumos.seed_terms == build_seed_terms(["cardhu 12"], ["whisky de malta"])
    assert insumos.excluded_terms == ["cardhu gin"]
    assert insumos.outras_marcas == ["glenfiddich"]
    assert (insumos.location, insumos.language) == ("ES", "es")


async def test_a_candidate_is_measured_by_the_same_rules_with_its_own_terms() -> None:
    """A régua só compara igual com igual se o candidato tem as mesmas sementes
    com a categoria, os vetos dele e o veto das concorrentes."""
    candidato = {"country": "ESP", "search_terms": ["cardhu whisky"], "excluded_terms": ["cardhu tour"]}
    insumos = await demand_inputs(_repo(), _ENTIDADE, _pais(), brand_key=_MARCA, candidate_market=candidato)

    assert insumos.search_terms == ["cardhu whisky"]
    assert insumos.category_terms == ["whisky de malta"]
    assert insumos.excluded_terms == ["cardhu tour"]
    assert insumos.outras_marcas == ["glenfiddich"]


async def test_a_market_without_terms_falls_back_to_its_name() -> None:
    repo = _repo()
    repo.markets_for = AsyncMock(return_value={"ESP": {"primary_name": "freidora de aire"}})
    insumos = await demand_inputs(repo, _ENTIDADE, _pais())

    assert insumos.search_terms == ["freidora de aire"]
    assert insumos.category_terms == [] and insumos.outras_marcas == []
