"""Builds each country's 5-dimension vector (Cap. 5 §5.5), reused by both
weight calibration (Entropy Weight needs a population of these vectors) and
the classical-library cross-validation (pymcdm's TOPSIS also needs a
population matrix) — see app/services/weight_calibration_service.py and
app/services/mos/library_reference.py.

Demand has no country-level indicator of its own (unlike GDP, population,
etc., which exist for any country regardless of product) — its country
value here is the average Demand sub-score across every product that
already has computed indicators for that country/period, growing
organically through normal usage (correction of 2026-07-03, see memory
marketmind_mos_engine: a product-specific Demand weight broke cross-product
comparability and required synchronous PyTrends collection per new
product)."""

import uuid
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.indicator_value import IndicatorValue
from app.domain.enums import IndicatorDimension, PeriodType
from app.repositories.indicator_repository import IndicatorRepository
from app.services.mos.dimension_mapping import compute_dimension_subscores


class CountryVectorBuilder:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._indicator_repo = IndicatorRepository(session)

    async def macro_vector(self, country_iso3: str, period_key: str) -> dict[IndicatorDimension, float]:
        """The 4 macro dimensions (product-agnostic, entity_key=NULL)."""
        values = await self._indicator_repo.get_values_for(
            entity_key=None, country=country_iso3, period_type=PeriodType.MONTH.value, period_key=period_key
        )
        return compute_dimension_subscores(values)

    async def demand_value(self, country_iso3: str, period_key: str) -> float | None:
        """Average Demand sub-score across every product already computed
        for this country/period. None if no product has data there yet."""
        stmt = select(IndicatorValue).where(
            IndicatorValue.entity_key.is_not(None),
            IndicatorValue.country == country_iso3,
            IndicatorValue.period_type == PeriodType.MONTH.value,
            IndicatorValue.period_key == period_key,
        )
        result = await self._session.execute(stmt)
        by_entity: dict[uuid.UUID, dict[str, IndicatorValue]] = defaultdict(dict)
        for iv in result.scalars().all():
            assert iv.entity_key is not None  # guaranteed by the entity_key.is_not(None) filter above
            by_entity[iv.entity_key][iv.indicator_code] = iv

        demand_scores = []
        for entity_values in by_entity.values():
            subscores = compute_dimension_subscores(entity_values)
            if IndicatorDimension.DEMAND in subscores:
                demand_scores.append(subscores[IndicatorDimension.DEMAND])

        if not demand_scores:
            return None
        return sum(demand_scores) / len(demand_scores)

    async def full_vector(self, country_iso3: str, period_key: str) -> dict[IndicatorDimension, float]:
        """All 5 dimensions for one country — may be a partial dict if some
        dimensions have insufficient data (caller decides how to handle
        missing keys, e.g. requiring a complete row before using it)."""
        vector = await self.macro_vector(country_iso3, period_key)
        demand = await self.demand_value(country_iso3, period_key)
        if demand is not None:
            vector[IndicatorDimension.DEMAND] = demand
        return vector
