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
        """The canonical `countries` table holds exactly the AVAILABLE analysis
        basket (markets Google Ads can measure — see docs/paises-disponiveis.md).
        Ordered by continent then name so the frontend can group + list them
        alphabetically directly from this order. Also the fixed reference
        population for weight calibration."""
        result = await self._session.execute(
            select(Country).order_by(Country.continent, Country.name)
        )
        return list(result.scalars().all())
