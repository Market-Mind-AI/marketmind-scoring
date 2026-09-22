"""A sentinela olha resultado, não processo.

As probes dizem se o processo está de pé. Em 22/09/2026 o refresh do World Bank
recoletava 288 indicadores por noite e terminava `Complete`, em verde, com nada
certo. Processo de pé, resultado errado — e nada via.
"""

import uuid
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.analysis_request import AnalysisRequest
from app.db.models.job import Job
from app.services.sentinela import Sentinela
from app.utils.time import utc_now


async def _relatorio(session: AsyncSession) -> dict:
    return (await Sentinela(session).verificar()).to_dict()


def _de(relatorio: dict, nome: str) -> dict:
    return next(v for v in relatorio["verificacoes"] if v["nome"] == nome)


async def test_an_analysis_past_its_own_deadline_is_a_finding(db_session: AsyncSession) -> None:
    """Trabalho que o comprador pagou e não recebeu. A varredura de retomada
    existe justamente para isso — se ainda assim sobrou uma, foi ela que
    falhou, e é o tipo de coisa que ninguém percebe olhando pods."""
    presa = AnalysisRequest(
        id=uuid.uuid4(),
        status="running",
        countries=["ESP"],
        created_at=utc_now() - timedelta(days=2),
        deadline_at=utc_now() - timedelta(hours=3),
    )
    db_session.add(presa)
    await db_session.flush()

    v = _de(await _relatorio(db_session), "analises_presas")

    assert v["ok"] is False
    assert str(presa.id) in v["detalhe"]


async def test_an_analysis_still_within_its_deadline_is_not(db_session: AsyncSession) -> None:
    """Uma análise pode ficar horas em `running` legitimamente, insistindo numa
    fonte fora do ar. O prazo é o que separa insistir de estar presa."""
    viva = AnalysisRequest(
        id=uuid.uuid4(),
        status="running",
        countries=["ESP"],
        created_at=utc_now(),
        deadline_at=utc_now() + timedelta(hours=20),
    )
    db_session.add(viva)
    await db_session.flush()

    v = _de(await _relatorio(db_session), "analises_presas")

    assert str(viva.id) not in v["detalhe"]


async def test_a_prewarm_that_stopped_running_is_a_finding(db_session: AsyncSession) -> None:
    """Quando ele para, toda análise volta a depender das fontes estarem de pé
    no minuto em que o usuário perguntou. Em 25/08/2026 a ONU ficou 29 minutos
    fora e nove países saíram de um ranking por causa disso."""
    antigo = Job(
        id=uuid.uuid4(),
        status="done",
        requested_by="macro_prewarm",
        request_payload={},
        created_at=utc_now() - timedelta(days=3),
    )
    db_session.add(antigo)
    await db_session.flush()

    v = _de(await _relatorio(db_session), "prewarm_recente")

    assert v["ok"] is False


async def test_a_recent_prewarm_passes(db_session: AsyncSession) -> None:
    db_session.add(
        Job(
            id=uuid.uuid4(),
            status="done",
            requested_by="macro_prewarm",
            request_payload={},
            created_at=utc_now(),
        )
    )
    await db_session.flush()

    v = _de(await _relatorio(db_session), "prewarm_recente")

    assert v["ok"] is True


async def test_a_check_that_blows_up_is_itself_a_finding(db_session: AsyncSession) -> None:
    """Uma verificação que quebra não pode passar por verificação que passou —
    seria o silêncio que este arquivo existe para acabar."""
    from unittest.mock import patch

    async def _prewarm_recente(self) -> None:  # noqa: ANN001 — precisa do mesmo nome e do self
        raise RuntimeError("banco fora")

    with patch.object(Sentinela, "_prewarm_recente", _prewarm_recente):
        relatorio = await _relatorio(db_session)

    v = _de(relatorio, "prewarm_recente")
    assert v["ok"] is False
    assert "a própria verificação falhou" in v["detalhe"]
    assert relatorio["ok"] is False, "e o relatório inteiro fica vermelho"
