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
    ) -> AnalysisRequest:
        row = AnalysisRequest(
            id=uuid.uuid4(),
            status="pending",
            entity_key=entity_key,
            canonical_title=canonical_title,
            countries=countries,
            estimated_price=None if estimated_price is None else Decimal(str(estimated_price)),
            price_currency=price_currency,
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

    async def mark_done(
        self, analysis_request: AnalysisRequest, *, result: list[dict[str, Any]], explanation: str | None = None
    ) -> None:
        analysis_request.status = "done"
        analysis_request.result = result
        analysis_request.explanation = explanation
        analysis_request.finished_at = utc_now()
        await self._session.flush()

    async def mark_failed(self, analysis_request: AnalysisRequest, *, error_message: str) -> None:
        analysis_request.status = "failed"
        analysis_request.error_message = error_message
        analysis_request.finished_at = utc_now()
        await self._session.flush()
