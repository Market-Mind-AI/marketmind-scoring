"""Retoma análises que morreram no meio, e encerra as que já passaram do prazo.

POR QUE EXISTE. A decisão D3 ("nada parcial sai") transformou uma falha de
fonte numa espera: a análise fica em `running`, insistindo, até o dado vir. Mas
a análise roda como `BackgroundTask` do FastAPI, dentro do processo — se o pod
reinicia, ela some e o pedido fica em `running` para sempre. Sem retomada,
trocamos "entrega incompleta" por "não entrega nunca", que é pior: o usuário
paga e não recebe nem o parcial.

COMO SE DISTINGUE MORTA DE OCUPADA. Pelo batimento (`heartbeat_at`, migration
034), atualizado a cada país coberto e a cada tentativa do laço de insistência.
Silêncio maior que `analysis_stale_after_seconds` é morte, porque é mais longo
que o maior passo entre dois batimentos. Só isso torna a varredura segura se um
dia rodar com mais de uma réplica: uma análise viva nunca é retomada em
paralelo com ela mesma.

POR QUE RETOMAR É SIMPLESMENTE RODAR DE NOVO. Todo passo de `AnalysisService.run`
é idempotente por construção, e não por acidente: a coleta pula o que a
`coverage_index` já tem, os indicadores são upsert por (país, indicador,
período), o MOS é upsert por (produto, país, período) e o campeão é cacheado
por período. Retomar é reexecutar e deixar cada passo reconhecer o que já
existe — nenhum estado intermediário precisou ser inventado para isso.

O prazo é verificado antes: uma análise cujo `deadline_at` já passou enquanto
estava interrompida não é retomada, é encerrada como `failed` com o motivo. É a
mesma regra que o laço aplicaria se estivesse vivo (D5).

Sem broker e sem worker dedicado, no mesmo espírito do SSE, que observa o banco
em vez de fila.
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
    retomadas: list[uuid.UUID] = field(default_factory=list)
    expiradas: list[uuid.UUID] = field(default_factory=list)


class AnalysisResumeService:
    async def sweep(self) -> SweepReport:
        """Uma passada: encerra o que passou do prazo, retoma o resto."""
        from app.services.analysis_service import AnalysisService  # ciclo: run() é quem retoma

        limite = utc_now() - timedelta(seconds=settings.analysis_stale_after_seconds)
        report = SweepReport()

        async with async_session_factory() as session:
            paradas = (
                (
                    await session.execute(
                        select(AnalysisRequest).where(
                            AnalysisRequest.status == "running",
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
            report.verificadas = len(paradas)
            expirar: list[AnalysisRequest] = []
            retomar: list[uuid.UUID] = []
            for pedido in paradas:
                if pedido.deadline_at is not None and pedido.deadline_at <= utc_now():
                    expirar.append(pedido)
                else:
                    retomar.append(pedido.id)

            repo = AnalysisRepository(session)
            for pedido in expirar:
                await repo.mark_failed(
                    pedido,
                    error_message=(
                        "A análise foi interrompida e o prazo de espera pela fonte de dados expirou enquanto "
                        "ela estava parada. Nada parcial é entregue; refaça o pedido."
                    ),
                )
                report.expiradas.append(pedido.id)
            if expirar:
                await session.commit()

        # Fora da sessão da varredura: cada retomada abre a sua.
        for request_id in retomar:
            calada_ha = settings.analysis_stale_after_seconds
            logger.warning(f"Retomando análise {request_id}, parada há mais de {calada_ha}s")
            async with async_session_factory() as session:
                await AnalysisService(session).run(request_id)
            report.retomadas.append(request_id)

        if report.verificadas:
            logger.info(
                f"Varredura de análises: {report.verificadas} paradas, "
                f"{len(report.retomadas)} retomadas, {len(report.expiradas)} expiradas"
            )
        return report

    async def run_forever(self) -> None:
        """Laço da varredura, ligado no lifespan da aplicação. A primeira passada
        é imediata: no startup, toda análise em `running` é órfã de um processo
        que não existe mais — é o caso mais comum e o mais fácil de perder."""
        intervalo = settings.analysis_sweep_interval_seconds
        if intervalo <= 0:
            logger.info("Varredura de análises paradas desligada (analysis_sweep_interval_seconds=0)")
            return
        while True:
            try:
                await self.sweep()
            except Exception:  # noqa: BLE001 — laço de fundo, nunca pode morrer por uma passada ruim
                logger.exception("Varredura de análises paradas falhou; tentando de novo no próximo intervalo")
            await asyncio.sleep(intervalo)
