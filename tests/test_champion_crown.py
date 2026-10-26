"""Quem coroa o campeão é quem tem a régua.

O orquestrador lia o campeão guardado na coleta dos candidatos, a régua subia
na chamada seguinte e apagava o campeão, e ele coroava assim mesmo o que já
tinha lido — medido contra a régua de antes.
"""

import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.services.analysis_service import AnalysisService

_ENTIDADE = SimpleNamespace(entity_key=uuid.uuid4())


def _servico(*, guardado: tuple[bool, str | None, Decimal | None], candidatos: list[tuple[str, float]]):  # noqa: ANN202
    servico = AnalysisService.__new__(AnalysisService)
    servico._session = AsyncMock()
    servico.measure_demand_ceiling = AsyncMock(return_value=(15800.0, {"mode": "reference"}, None))
    servico.score_reference_candidates = AsyncMock(
        return_value=[SimpleNamespace(country=c, mos=Decimal(str(m))) for c, m in candidatos]
    )
    servico._product_repo = SimpleNamespace(
        champion_for=AsyncMock(return_value=guardado), set_champion=AsyncMock()
    )
    servico._mos_repo = SimpleNamespace(get_mos_result=AsyncMock(return_value=SimpleNamespace(country="ESP")))
    return servico


async def _coroar(servico, pedidos: list[tuple[str, float]]):  # noqa: ANN001, ANN202
    return await servico.crown_champion(
        entity=_ENTIDADE,
        requested=[c for c, _m in pedidos],
        requested_results=[{"country": c, "mos": m} for c, m in pedidos],
        period_key="2026-10",
    )


async def test_a_candidate_above_every_requested_market_is_crowned_with_its_row() -> None:
    servico = _servico(guardado=(False, None, None), candidatos=[("USA", 71.0), ("IND", 40.0)])

    coroado = await _coroar(servico, [("BRA", 64.0)])

    assert (coroado.country, coroado.mos, coroado.requested) == ("USA", 71.0, False)
    assert coroado.row is not None and coroado.row.country == "USA"
    kwargs = servico._product_repo.set_champion.await_args.kwargs
    assert (kwargs["country"], kwargs["demand_ceiling"]) == ("USA", 15800.0), "guardado com a régua de agora"


async def test_a_champion_stored_for_this_ruler_is_reused_without_scoring() -> None:
    servico = _servico(guardado=(True, "ESP", Decimal("66.37")), candidatos=[])

    coroado = await _coroar(servico, [("BRA", 64.0)])

    servico.score_reference_candidates.assert_not_awaited()
    assert (coroado.country, coroado.requested, coroado.from_cache) == ("ESP", False, True)
    servico._product_repo.set_champion.assert_not_awaited()


async def test_a_requested_market_that_beats_the_stored_one_replaces_it() -> None:
    """Medido: um campeão guardado a 66,37 ficou coroado numa análise em que um
    mercado pedido tinha 69."""
    servico = _servico(guardado=(True, "ESP", Decimal("66.37")), candidatos=[])

    coroado = await _coroar(servico, [("BRA", 69.0)])

    assert (coroado.country, coroado.requested, coroado.row) == ("BRA", True, None)
    assert servico._product_repo.set_champion.await_args.kwargs["country"] == "BRA"


async def test_a_tie_goes_to_the_requested_market() -> None:
    servico = _servico(guardado=(False, None, None), candidatos=[("USA", 64.0)])

    coroado = await _coroar(servico, [("BRA", 64.0)])

    assert coroado.country == "BRA"


async def test_nothing_scorable_is_stored_as_an_answer() -> None:
    servico = _servico(guardado=(False, None, None), candidatos=[])

    coroado = await _coroar(servico, [("BRA", None)])  # type: ignore[list-item]

    assert coroado.country is None
    assert servico._product_repo.set_champion.await_args.kwargs["country"] is None
