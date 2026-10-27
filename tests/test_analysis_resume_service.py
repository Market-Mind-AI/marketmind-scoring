"""Encerramento de análise parada que passou do prazo.

Retomar é do orchestrator, pelo claim (2026-10-27): duas regras de retomada, uma
aqui e outra lá, podiam rodar a mesma análise duas vezes. Aqui fica a regra de
status — uma análise parada cujo prazo venceu termina em `failed` com o motivo.
"""

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models.analysis_request import AnalysisRequest
from app.services.analysis_resume_service import AnalysisResumeService
from app.utils.time import utc_now


async def _pedido(
    session: AsyncSession,
    *,
    status: str = "running",
    heartbeat_min_atras: float | None = None,
    deadline_min: float | None = None,
) -> AnalysisRequest:
    pedido = AnalysisRequest(
        id=uuid.uuid4(),
        status=status,
        entity_key=None,
        canonical_title=f"produto {uuid.uuid4().hex[:6]}",
        countries=["USA"],
        created_at=utc_now() - timedelta(hours=2),
        heartbeat_at=None if heartbeat_min_atras is None else utc_now() - timedelta(minutes=heartbeat_min_atras),
        deadline_at=None if deadline_min is None else utc_now() + timedelta(minutes=deadline_min),
    )
    session.add(pedido)
    await session.commit()
    return pedido


async def test_prazo_vencido_encerra(db_session: AsyncSession) -> None:
    """Mesma regra que o laço aplicaria se estivesse vivo (D5): passou o
    orçamento, a análise termina em `failed` com motivo — nunca `done`
    incompleta."""
    pedido = await _pedido(db_session, heartbeat_min_atras=60, deadline_min=-5)

    report = await AnalysisResumeService().sweep()

    assert pedido.id in report.expiradas
    await db_session.refresh(pedido)
    assert pedido.status == "failed"
    assert "prazo de espera" in (pedido.error_message or "")


async def test_analise_parada_dentro_do_prazo_nao_e_tocada_nem_retomada(db_session: AsyncSession) -> None:
    """Retomar é do orchestrator. Uma análise calada que ainda tem prazo fica
    como está, e a varredura nunca reinvoca o grafo."""
    pedido = await _pedido(db_session, heartbeat_min_atras=60, deadline_min=120)

    rodou = AsyncMock()
    with patch("app.services.analysis_service.AnalysisService.run", rodou):
        report = await AnalysisResumeService().sweep()

    assert pedido.id not in report.expiradas
    rodou.assert_not_awaited()
    await db_session.refresh(pedido)
    assert pedido.status == "running"


async def test_analise_viva_nao_e_encerrada_mesmo_vencida(db_session: AsyncSession) -> None:
    """O batimento separa "morta" de "ocupada": quem está rodando encerra a si
    mesmo quando o prazo passa."""
    pedido = await _pedido(db_session, heartbeat_min_atras=1, deadline_min=-5)

    report = await AnalysisResumeService().sweep()

    assert pedido.id not in report.expiradas


async def test_analise_terminada_nao_e_tocada(db_session: AsyncSession) -> None:
    """Só `running` é candidata. Uma análise entregue não muda de status."""
    await _pedido(db_session, status="done", heartbeat_min_atras=600, deadline_min=-5)
    await _pedido(db_session, status="failed", heartbeat_min_atras=600, deadline_min=-5)

    report = await AnalysisResumeService().sweep()

    assert report.verificadas == 0


async def test_analise_sem_batimento_usa_a_data_de_criacao(db_session: AsyncSession) -> None:
    """Batimento nulo é análise que morreu antes do primeiro sinal de vida — ou
    de antes da migration 034. `created_at` é o único relógio que ela tem, e
    ignorá-la deixaria justamente as mais antigas presas para sempre."""
    pedido = await _pedido(db_session, heartbeat_min_atras=None, deadline_min=-5)

    report = await AnalysisResumeService().sweep()

    assert pedido.id in report.expiradas


async def test_varredura_desligada_nao_roda(db_session: AsyncSession) -> None:
    """Intervalo zero desliga o laço — o que um teste precisa poder fazer sem
    inventar um caminho alternativo no código de produção."""
    with patch.object(settings, "analysis_sweep_interval_seconds", 0):
        await AnalysisResumeService().run_forever()  # retorna em vez de girar
