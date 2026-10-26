"""Quais fontes têm este país, e o que fazer com as que não têm."""

from types import SimpleNamespace

from app.domain.enums import DataSource
from app.services.macro_coverage import fontes_sem_cobertura_de, macro_gaps


def _pais(**campos) -> SimpleNamespace:
    base = {
        "iso3": "BRA",
        "iso2": "BR",
        "worldbank_country_id": None,
        "pytrends_geo": None,
        "oecd_ref_area": "BRA",
        "undata_location_id": "076",
    }
    return SimpleNamespace(**{**base, **campos})


def test_a_country_the_oecd_does_not_cover_is_not_asked() -> None:
    """Argentina e Peru não têm série de preços imobiliários na OCDE, e eram
    duas tarefas falhando todas as noites desde sempre. Uma falha que nunca vai
    deixar de acontecer ensina quem lê o log a ignorar falhas."""
    sem = fontes_sem_cobertura_de(_pais(iso3="ARG", oecd_ref_area=None))

    assert DataSource.OECD in sem
    fontes = {g.source for g in macro_gaps("ARG", fontes_sem_cobertura=sem)}
    assert DataSource.OECD not in fontes


def test_the_other_sources_of_that_country_are_still_asked() -> None:
    """Não cobrir um indicador não é não cobrir o país. A Argentina continua
    pontuada com os outros 21 indicadores."""
    sem = fontes_sem_cobertura_de(_pais(iso3="ARG", oecd_ref_area=None))
    fontes = {g.source for g in macro_gaps("ARG", fontes_sem_cobertura=sem)}

    assert DataSource.WORLDBANK in fontes
    assert DataSource.UNDATA in fontes


def test_a_covered_country_is_asked_for_everything() -> None:
    sem = fontes_sem_cobertura_de(_pais())

    assert sem == frozenset()
    fontes = {g.source for g in macro_gaps("BRA", fontes_sem_cobertura=sem)}
    assert fontes == {DataSource.WORLDBANK, DataSource.OECD, DataSource.UNDATA}


def test_a_country_with_no_mapping_at_all_is_treated_as_uncovered() -> None:
    """O lado seguro do erro: um país acrescentado à tabela sem mapeamento nasce
    como não coberto e é verificado por alguém, em vez de gerar uma falha
    noturna que ninguém vai investigar."""
    sem = fontes_sem_cobertura_de(_pais(oecd_ref_area=None, undata_location_id=None))

    assert sem == frozenset({DataSource.OECD, DataSource.UNDATA})
