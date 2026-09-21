"""Quem está executando uma análise agora.

O `status` sozinho não responde isso. Uma análise fica `running` por horas
legitimamente, e `running` não distingue "há um processo trabalhando nisso" de
"o processo que trabalhava nisso morreu com o pod". Sem essa distinção a
retomada só podia agir por silêncio — quinze minutos com o usuário olhando uma
tela que diz "em andamento" e nada andando.
"""

import uuid
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.analysis_request import AnalysisRequest
from app.repositories.analysis_repository import AnalysisRepository
from app.utils.time import utc_now

_STALE = 120


async def _running(session: AsyncSession, **campos) -> AnalysisRequest:
    pedido = AnalysisRequest(
        id=uuid.uuid4(), status="running", countries=["ESP"], created_at=utc_now(), **campos
    )
    session.add(pedido)
    await session.flush()
    return pedido


async def test_an_analysis_nobody_holds_is_orphaned(db_session: AsyncSession) -> None:
    pedido = await _running(db_session)

    orfas = await AnalysisRepository(db_session).list_unclaimed_running(claim_stale_after_seconds=_STALE)

    assert pedido.id in [o.id for o in orfas]


async def test_an_analysis_a_live_process_holds_is_not_orphaned(db_session: AsyncSession) -> None:
    """A reivindicação é renovada a cada progresso, então uma recente significa
    que alguém está trabalhando — e retomá-la seria executá-la duas vezes."""
    pedido = await _running(db_session, claimed_by="agente-a", claimed_at=utc_now())

    orfas = await AnalysisRepository(db_session).list_unclaimed_running(claim_stale_after_seconds=_STALE)

    assert pedido.id not in [o.id for o in orfas]


async def test_a_claim_that_stopped_moving_belongs_to_a_dead_process(db_session: AsyncSession) -> None:
    pedido = await _running(
        db_session, claimed_by="agente-a", claimed_at=utc_now() - timedelta(seconds=_STALE + 60)
    )

    orfas = await AnalysisRepository(db_session).list_unclaimed_running(claim_stale_after_seconds=_STALE)

    assert pedido.id in [o.id for o in orfas]


async def test_two_agents_racing_for_the_same_analysis_and_only_one_runs(db_session: AsyncSession) -> None:
    """É isto que permite duas réplicas. Sem a disputa, um pod que sobe retoma o
    que o outro está executando e a mesma análise roda duas vezes."""
    pedido = await _running(db_session)
    repo = AnalysisRepository(db_session)

    primeiro = await repo.claim(pedido, runner="agente-a", claim_stale_after_seconds=_STALE)
    segundo = await repo.claim(pedido, runner="agente-b", claim_stale_after_seconds=_STALE)

    assert primeiro is True
    assert segundo is False
    assert pedido.claimed_by == "agente-a"


async def test_the_same_agent_can_retake_what_it_already_holds(db_session: AsyncSession) -> None:
    """Retomar a própria análise é reentrada, não disputa: o mesmo pod pode
    reinvocar o grafo pelo mesmo analysis_id e isso é o que 'retomar' significa."""
    pedido = await _running(db_session)
    repo = AnalysisRepository(db_session)

    assert await repo.claim(pedido, runner="agente-a", claim_stale_after_seconds=_STALE) is True
    assert await repo.claim(pedido, runner="agente-a", claim_stale_after_seconds=_STALE) is True


async def test_a_dead_agents_analysis_can_be_taken_over(db_session: AsyncSession) -> None:
    pedido = await _running(
        db_session, claimed_by="agente-morto", claimed_at=utc_now() - timedelta(seconds=_STALE + 60)
    )

    assert (
        await AnalysisRepository(db_session).claim(
            pedido, runner="agente-novo", claim_stale_after_seconds=_STALE
        )
        is True
    )
    assert pedido.claimed_by == "agente-novo"


async def test_a_finished_analysis_is_never_orphaned(db_session: AsyncSession) -> None:
    """Só `running` conta. Retomar uma análise entregue a executaria de novo e
    cobraria de novo."""
    pedido = await _running(db_session)
    pedido.status = "done"
    await db_session.flush()

    orfas = await AnalysisRepository(db_session).list_unclaimed_running(claim_stale_after_seconds=_STALE)

    assert pedido.id not in [o.id for o in orfas]
