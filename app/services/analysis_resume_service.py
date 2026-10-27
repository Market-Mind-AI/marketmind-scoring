"""Encerra as análises que passaram do prazo enquanto estavam paradas.

RETOMAR NÃO É MAIS DAQUI (2026-10-27). Esta varredura reinvocava o grafo de
toda análise calada há mais de `analysis_stale_after_seconds`, enquanto o
orchestrator tinha a própria vigia de órfãs, com outra regra de vida (um claim
que vence em 120 s). Duas regras para a mesma pergunta deixavam uma análise
retomada aqui ser tomada de novo pela vigia do orchestrator e rodar duas vezes.
Quem decide e retoma é o orchestrator, pelo claim; aqui fica só o que é regra de
status, e status é do scoring.

O PRAZO. Uma análise cujo `deadline_at` passou enquanto estava parada não é
retomada por ninguém: é encerrada como `failed` com o motivo. É a mesma regra
que o laço aplicaria se estivesse vivo (D5).

COMO SE DISTINGUE MORTA DE OCUPADA. Pelo batimento (`heartbeat_at`, migration
034), carimbado a cada vez que o agente diz o que está fazendo. Uma análise viva
nunca é encerrada por esta varredura, mesmo depois do prazo: quem está rodando
encerra a si mesmo.
"""

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import or_, select

from app.core.config import settings
from app.core.logging import logger
from app.db.models.analysis_request import AnalysisRequest
from app.db.session import async_session_factory
from app.repositories.analysis_repository import AnalysisRepository
from app.utils.time import utc_now


@dataclass
class SweepReport:
    verificadas: int = 0
    expiradas: list[uuid.UUID] = field(default_factory=list)


class AnalysisResumeService:
    async def sweep(self) -> SweepReport:
        """Uma passada: encerra as paradas cujo prazo já passou."""
        limite = utc_now() - timedelta(seconds=settings.analysis_stale_after_seconds)
        report = SweepReport()

        async with async_session_factory() as session:
            vencidas = (
                (
                    await session.execute(
                        select(AnalysisRequest).where(
                            AnalysisRequest.status == "running",
                            AnalysisRequest.deadline_at.is_not(None),
                            AnalysisRequest.deadline_at <= utc_now(),
                            # Batimento nulo é análise que morreu antes do
                            # primeiro sinal de vida — ou de antes da migration
                            # 034. `created_at` é o único relógio que ela tem.
                            or_(
                                AnalysisRequest.heartbeat_at < limite,
                                AnalysisRequest.heartbeat_at.is_(None) & (AnalysisRequest.created_at < limite),
                            ),
                        )
                    )
                )
                .scalars()
                .all()
            )
            report.verificadas = len(vencidas)
            repo = AnalysisRepository(session)
            for pedido in vencidas:
                await repo.mark_failed(
                    pedido,
                    error_message=(
                        "A análise foi interrompida e o prazo de espera pela fonte de dados expirou enquanto "
                        "ela estava parada. Nada parcial é entregue; refaça o pedido."
                    ),
                )
                report.expiradas.append(pedido.id)
            if vencidas:
                await session.commit()

        if report.expiradas:
            logger.info(f"Varredura de análises: {len(report.expiradas)} expiradas")
        return report

    async def run_forever(self) -> None:
        """Laço da varredura, ligado no lifespan da aplicação."""
        intervalo = settings.analysis_sweep_interval_seconds
        if intervalo <= 0:
            logger.info("Varredura de análises vencidas desligada (analysis_sweep_interval_seconds=0)")
            return
        while True:
            try:
                await self.sweep()
            except Exception:  # noqa: BLE001 — laço de fundo, nunca pode morrer por uma passada ruim
                logger.exception("Varredura de análises vencidas falhou; tentando de novo no próximo intervalo")
            await asyncio.sleep(intervalo)
