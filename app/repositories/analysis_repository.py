import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.analysis_request import AnalysisRequest
from app.utils.time import utc_now

# Os estados de que uma análise não sai mais. As transições moram aqui, e não
# em quem chama: eram três chamadores, cada um com o seu `if status not in`, e
# o que não tinha deixava uma análise entregue virar falha.
TERMINAL_STATUSES: frozenset[str] = frozenset({"done", "failed"})


class AnalysisRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        entity_key: uuid.UUID | None,
        canonical_title: str | None,
        countries: list[str],
        estimated_price: Decimal | float | None,
        price_currency: str | None,
        user_prompt: str | None = None,
        catalog_method: str | None = None,
        catalog_match_score: Decimal | float | None = None,
        catalog_match_reasoning: str | None = None,
        catalog_query_text: str | None = None,
        brand_key: uuid.UUID | None = None,
        brand_name: str | None = None,
        language: str | None = None,
    ) -> AnalysisRequest:
        row = AnalysisRequest(
            id=uuid.uuid4(),
            status="pending",
            entity_key=entity_key,
            canonical_title=canonical_title,
            countries=countries,
            estimated_price=None if estimated_price is None else Decimal(str(estimated_price)),
            price_currency=price_currency,
            user_prompt=user_prompt,
            catalog_method=catalog_method,
            catalog_match_score=None if catalog_match_score is None else Decimal(str(catalog_match_score)),
            catalog_match_reasoning=catalog_match_reasoning,
            catalog_query_text=catalog_query_text,
            brand_key=brand_key,
            brand_name=brand_name,
            language=language,
            created_at=utc_now(),
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(self, request_id: uuid.UUID) -> AnalysisRequest | None:
        stmt = select(AnalysisRequest).where(AnalysisRequest.id == request_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_unclaimed_running(self, *, claim_stale_after_seconds: int) -> list[AnalysisRequest]:
        """Análises em curso que ninguém está executando de verdade.

        Sem reivindicação, ou com uma velha demais para ser de um processo vivo
        — `claimed_at` é renovado a cada progresso, então reivindicação parada é
        processo morto.
        """
        limite = utc_now() - timedelta(seconds=claim_stale_after_seconds)
        stmt = select(AnalysisRequest).where(
            AnalysisRequest.status == "running",
            or_(AnalysisRequest.claimed_at.is_(None), AnalysisRequest.claimed_at < limite),
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def claim(self, analysis_request: AnalysisRequest, *, runner: str, claim_stale_after_seconds: int) -> bool:
        """Tenta assumir a execução. False quando outro processo vivo já a tem.

        É o que permite dois agentes lado a lado: o que perde a disputa
        simplesmente não roda, em vez de executar a mesma análise em paralelo.
        """
        agora = utc_now()
        dono = analysis_request.claimed_by
        desde = analysis_request.claimed_at
        viva = desde is not None and (agora - desde).total_seconds() < claim_stale_after_seconds
        if viva and dono != runner:
            return False
        analysis_request.claimed_by = runner
        analysis_request.claimed_at = agora
        await self._session.flush()
        return True

    async def mark_running(self, analysis_request: AnalysisRequest) -> bool:
        """False, e nada muda, para uma análise já terminada: retomar uma
        concluída a punha de volta em `running` e a tela voltava a esperar."""
        if analysis_request.status in TERMINAL_STATUSES:
            return False
        analysis_request.status = "running"
        await self._session.flush()
        return True

    async def set_explanation(self, analysis_request: AnalysisRequest, explanation: str | None) -> None:
        """Attach the narrative to an already-completed analysis.

        Separate from mark_done because the narrative is not what the caller is
        waiting for. Generating it took 249s of a 192s analysis measured against
        gpt-5 — longer than everything else combined, and by then every score was
        already computed. So the request reaches "done" with its numbers and the
        text arrives here, shortly after.

        Deliberately does not touch status or finished_at: the analysis was
        finished when the scores were. A null explanation stays null, which is
        the same contract as before — the narrative has always been best-effort.
        """
        analysis_request.explanation = explanation
        await self._session.flush()

    async def mark_done(
        self,
        analysis_request: AnalysisRequest,
        *,
        result: list[dict[str, Any]],
        explanation: str | None = None,
        breakdown: dict[str, Any] | None = None,
    ) -> bool:
        """False, e nada muda, para uma análise que já falhou: o comprador já
        leu o motivo, e a cobrança é amarrada a `done`. Concluir de novo uma
        concluída é permitido — retomar é refazer, e o resultado é o mesmo."""
        if analysis_request.status == "failed":
            return False
        analysis_request.status = "done"
        analysis_request.result = result
        analysis_request.explanation = explanation
        analysis_request.breakdown = breakdown
        analysis_request.finished_at = utc_now()
        # A finished analysis is not waiting on anything. Every done row in the
        # database still carried the last thing it had been doing ("scoring the
        # markets"), which is a lie to anyone reading the row and a lie on any
        # screen that shows it.
        analysis_request.progress = None
        await self._session.flush()
        return True

    async def mark_failed(self, analysis_request: AnalysisRequest, *, error_message: str) -> bool:
        """False, e nada muda, para uma análise já terminada. Uma concluída
        nunca vira falha — ela foi entregue e cobrada, e uma falha tardia de
        quem a executava (a narrativa, uma retomada, a varredura) não desfaz
        isso. Uma que já falhou guarda o primeiro motivo, que é o que a causou."""
        if analysis_request.status in TERMINAL_STATUSES:
            return False
        analysis_request.status = "failed"
        analysis_request.error_message = error_message
        analysis_request.finished_at = utc_now()
        analysis_request.progress = None
        await self._session.flush()
        return True
