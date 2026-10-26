"""Mercado sem demanda é um fato do dado, por produto E marca, na janela da
demanda — não uma lembrança do orquestrador por produto."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.services import analysis_service
from app.services.analysis_service import AnalysisService
from app.services.demand_inputs import DemandInputs


def _servico(volume: float | None, guardado: dict) -> AnalysisService:  # type: ignore[type-arg]
    servico = AnalysisService.__new__(AnalysisService)
    servico._insumos = AsyncMock(return_value=DemandInputs("JPN", "JP", "ja", ["air fryer"], [], ["air fryer"], [], []))
    servico._volume_de_busca = AsyncMock(return_value=volume)
    return servico


async def _ausencia(volume: float | None, guardado: dict) -> dict:  # type: ignore[type-arg]
    servico = _servico(volume, guardado)
    with patch.object(analysis_service, "pytrends_stored_volume", AsyncMock(return_value=guardado)):
        return await servico.demand_absence(SimpleNamespace(entity_key=uuid.uuid4()), "JPN", period_key="2026-10")


async def test_every_term_answered_and_none_with_volume_is_absent() -> None:
    resposta = await _ausencia(
        None, {"keywords": [{"keyword": "air fryer", "collected_at": "2026-10-20"}], "missing": []}
    )

    assert (resposta["absent"], resposta["checked_at"]) == (True, "2026-10-20")


async def test_a_term_still_to_collect_is_not_knowing() -> None:
    resposta = await _ausencia(None, {"keywords": [], "missing": ["air fryer"]})

    assert resposta["absent"] is False


async def test_volume_means_not_absent() -> None:
    resposta = await _ausencia(1200.0, {"keywords": [{"keyword": "air fryer"}], "missing": []})

    assert resposta["absent"] is False
