import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.indicator_weight import IndicatorWeight
from app.db.models.mos_result import MosResult
from app.utils.time import utc_now


class MosRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_weight(self, indicator_code: str, period_key: str) -> IndicatorWeight | None:
        stmt = select(IndicatorWeight).where(
            IndicatorWeight.indicator_code == indicator_code,
            IndicatorWeight.period_key == period_key,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def upsert_weight(
        self,
        *,
        indicator_code: str,
        dimension: str,
        period_key: str,
        weight: Decimal | float,
        is_closed: bool,
    ) -> IndicatorWeight:
        existing = await self.get_weight(indicator_code, period_key)
        if existing is not None:
            if existing.is_closed:
                # Immutable once closed (marketmind_mos_engine: same
                # guarantee as normalization_reference).
                return existing
            existing.weight = Decimal(str(weight))
            existing.is_closed = is_closed
            existing.computed_at = utc_now()
            await self._session.flush()
            return existing

        row = IndicatorWeight(
            id=uuid.uuid4(),
            indicator_code=indicator_code,
            dimension=dimension,
            period_key=period_key,
            weight=Decimal(str(weight)),
            is_closed=is_closed,
            computed_at=utc_now(),
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_mos_result(
        self, entity_key: uuid.UUID, country: str, period_type: str, period_key: str
    ) -> MosResult | None:
        stmt = select(MosResult).where(
            MosResult.entity_key == entity_key,
            MosResult.country == country,
            MosResult.period_type == period_type,
            MosResult.period_key == period_key,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def upsert_mos_result(
        self,
        *,
        entity_key: uuid.UUID,
        country: str,
        period_type: str,
        period_key: str,
        mos: Decimal | float | None,
        mos_before_cap: Decimal | float | None,
        capped_by: str | None,
        topsis_score: Decimal | float | None,
        library_topsis_score: Decimal | float | None = None,
        library_mos: Decimal | float | None = None,
        mos_interpretation: str | None = None,
        explanation: str | None = None,
        availability_status: str,
        excluded_reason: str | None,
    ) -> MosResult:
        existing = await self.get_mos_result(entity_key, country, period_type, period_key)
        mos_decimal = None if mos is None else Decimal(str(mos))
        mos_before_cap_decimal = None if mos_before_cap is None else Decimal(str(mos_before_cap))
        topsis_decimal = None if topsis_score is None else Decimal(str(topsis_score))
        library_topsis_decimal = None if library_topsis_score is None else Decimal(str(library_topsis_score))
        library_mos_decimal = None if library_mos is None else Decimal(str(library_mos))

        if existing is not None:
            existing.mos = mos_decimal
            existing.mos_before_cap = mos_before_cap_decimal
            existing.capped_by = capped_by
            existing.topsis_score = topsis_decimal
            existing.library_topsis_score = library_topsis_decimal
            existing.library_mos = library_mos_decimal
            existing.mos_interpretation = mos_interpretation
            existing.explanation = explanation
            existing.availability_status = availability_status
            existing.excluded_reason = excluded_reason
            existing.computed_at = utc_now()
            await self._session.flush()
            return existing

        row = MosResult(
            id=uuid.uuid4(),
            entity_key=entity_key,
            country=country,
            period_type=period_type,
            period_key=period_key,
            mos=mos_decimal,
            mos_before_cap=mos_before_cap_decimal,
            capped_by=capped_by,
            topsis_score=topsis_decimal,
            library_topsis_score=library_topsis_decimal,
            library_mos=library_mos_decimal,
            mos_interpretation=mos_interpretation,
            explanation=explanation,
            availability_status=availability_status,
            excluded_reason=excluded_reason,
            computed_at=utc_now(),
        )
        self._session.add(row)
        await self._session.flush()
        return row
