"""O que se mostra de um resultado sai de um lugar só: as marcas de cada linha,
a ordem dos países e o destaque."""

from app.services.presentation import display_order, headline_country, present_rows

_LINHAS = [
    {"country": "BRA", "mos": 42.0},
    {"country": "MEX", "mos": None},
    {"country": "ESP", "mos": 69.0},
    {"country": "USA", "mos": 71.04},
]
_PEDIDOS = ["BRA", "MEX", "ESP"]


def test_the_off_request_champion_comes_first_then_the_requested_by_mos() -> None:
    assert display_order(_LINHAS, _PEDIDOS) == ["USA", "ESP", "BRA", "MEX"]


def test_the_headline_is_the_best_requested_market() -> None:
    assert headline_country(_LINHAS, _PEDIDOS) == "ESP"


def test_every_row_is_flagged_and_shown_in_the_analysis_language() -> None:
    linhas = {r["country"]: r for r in present_rows(_LINHAS, requested=_PEDIDOS, language="pt")}

    assert (linhas["USA"]["requested"], linhas["USA"]["is_champion"]) == (False, True)
    assert (linhas["ESP"]["requested"], linhas["ESP"]["is_champion"]) == (True, False)
    assert (linhas["USA"]["mos_display"], linhas["USA"]["mos_band_label"]) == (71.0, "Boa oportunidade")
    assert linhas["MEX"]["mos_band_key"] is None


def test_a_tie_crowns_the_requested_market() -> None:
    linhas = present_rows(
        [{"country": "USA", "mos": 64.0}, {"country": "BRA", "mos": 64.0}], requested=["BRA"], language="en"
    )

    assert [r["country"] for r in linhas if r["is_champion"]] == ["BRA"]
