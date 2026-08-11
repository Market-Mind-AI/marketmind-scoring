import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.analysis_request import AnalysisRequest
from app.utils.time import utc_now


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
            created_at=utc_now(),
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(self, request_id: uuid.UUID) -> AnalysisRequest | None:
        stmt = select(AnalysisRequest).where(AnalysisRequest.id == request_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def mark_running(self, analysis_request: AnalysisRequest) -> None:
        analysis_request.status = "running"
        await self._session.flush()

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
    ) -> None:
        analysis_request.status = "done"
        analysis_request.result = result
        analysis_request.explanation = explanation
        analysis_request.breakdown = breakdown
        analysis_request.finished_at = utc_now()
        await self._session.flush()

    async def mark_failed(self, analysis_request: AnalysisRequest, *, error_message: str) -> None:
        analysis_request.status = "failed"
        analysis_request.error_message = error_message
        analysis_request.finished_at = utc_now()
        await self._session.flush()
