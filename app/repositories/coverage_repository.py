import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.coverage_index import CoverageIndex
from app.utils.time import utc_now


class CoverageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(
        self,
        source: str,
        entity_key: uuid.UUID | None,
        country: str,
        indicator_code: str,
        period_key: str,
    ) -> CoverageIndex | None:
        stmt = select(CoverageIndex).where(
            CoverageIndex.source == source,
            CoverageIndex.entity_key == entity_key,
            CoverageIndex.country == country,
            CoverageIndex.indicator_code == indicator_code,
            CoverageIndex.period_key == period_key,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    def is_fresh(row: CoverageIndex | None) -> bool:
        """A closed period is collected once and never recollected — always
        fresh. An open period is fresh only within its ttl_days window; with
        no ttl_days it's treated as always stale (must be recollected on
        every check), matching the "mês corrente aberto, atualizado sob
        demanda" rule in marketmind_core_architecture."""
        if row is None:
            return False
        if row.is_closed:
            return True
        if row.ttl_days is None:
            return False
        return utc_now() < row.last_fetched_at + timedelta(days=row.ttl_days)

    async def upsert_collected(
        self,
        *,
        source: str,
        entity_key: uuid.UUID | None,
        country: str,
        indicator_code: str,
        period_key: str,
        is_closed: bool,
        ttl_days: int | None,
    ) -> CoverageIndex:
        existing = await self.get(source, entity_key, country, indicator_code, period_key)
        now = utc_now()
        if existing is not None:
            existing.is_closed = is_closed
            existing.last_fetched_at = now
            existing.ttl_days = ttl_days
            await self._session.flush()
            return existing

        row = CoverageIndex(
            id=uuid.uuid4(),
            source=source,
            entity_key=entity_key,
            country=country,
            indicator_code=indicator_code,
            period_key=period_key,
            is_closed=is_closed,
            last_fetched_at=now,
            ttl_days=ttl_days,
        )
        self._session.add(row)
        await self._session.flush()
        return row
