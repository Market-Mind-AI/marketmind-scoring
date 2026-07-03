from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.country import Country


class CountryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_iso3(self, iso3: str) -> Country | None:
        result = await self._session.execute(select(Country).where(Country.iso3 == iso3))
        return result.scalar_one_or_none()

    async def list_all(self) -> list[Country]:
        """The canonical `countries` table currently holds exactly the ~27
        reference basket seeded by migration 003 — used by
        weight_calibration_service as the fixed reference population for
        Entropy Weight calibration."""
        result = await self._session.execute(select(Country))
        return list(result.scalars().all())
