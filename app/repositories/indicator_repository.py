import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.indicator_value import IndicatorValue
from app.db.models.normalization_reference import NormalizationReference
from app.utils.time import utc_now


class IndicatorRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_values_for(
        self, *, entity_key: uuid.UUID | None, country: str, period_type: str, period_key: str
    ) -> dict[str, IndicatorValue]:
        """All indicator_values rows for one (entity, country, period) —
        used by the MOS engine to build a country's dimension vector.
        entity_key=None returns only country-level (product-agnostic) rows."""
        stmt = select(IndicatorValue).where(
            IndicatorValue.entity_key == entity_key,
            IndicatorValue.country == country,
            IndicatorValue.period_type == period_type,
            IndicatorValue.period_key == period_key,
        )
        result = await self._session.execute(stmt)
        return {iv.indicator_code: iv for iv in result.scalars().all()}

    async def get_reference(self, indicator_code: str, period_key: str) -> NormalizationReference | None:
        stmt = select(NormalizationReference).where(
            NormalizationReference.indicator_code == indicator_code,
            NormalizationReference.period_key == period_key,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def widen_reference_bounds(
        self, indicator_code: str, period_key: str, observed_value: float
    ) -> NormalizationReference | None:
        """A fixed-bounds indicator's normalization_reference row self-
        calibrates: a real observed value outside today's [min_x, max_x]
        widens the stored bound in place (never narrows), so the absolute
        yardstick only gets more accurate over time instead of silently
        clipping a real outlier at a guessed limit. Deliberately ignores
        is_closed — that flag protects upsert_reference's percentile
        snapshots from monthly drift, a different mechanism with a
        different lifecycle than a fixed anchor's self-widening."""
        reference = await self.get_reference(indicator_code, period_key)
        if reference is None:
            return None
        changed = False
        if observed_value < float(reference.min_x):
            reference.min_x = Decimal(str(observed_value))
            changed = True
        if observed_value > float(reference.max_x):
            reference.max_x = Decimal(str(observed_value))
            changed = True
        if changed:
            reference.computed_at = utc_now()
            await self._session.flush()
        return reference

    async def upsert_reference(
        self,
        *,
        indicator_code: str,
        period_key: str,
        min_x: Decimal | float,
        max_x: Decimal | float,
        reference_population_size: int,
        is_closed: bool,
    ) -> NormalizationReference:
        existing = await self.get_reference(indicator_code, period_key)
        if existing is not None:
            if existing.is_closed:
                # Immutable once closed — recomputing later must never
                # overwrite it (marketmind_mos_engine: "imutabilidade de
                # referência/pesos por período").
                return existing
            existing.min_x = Decimal(str(min_x))
            existing.max_x = Decimal(str(max_x))
            existing.reference_population_size = reference_population_size
            existing.is_closed = is_closed
            existing.computed_at = utc_now()
            await self._session.flush()
            return existing

        row = NormalizationReference(
            id=uuid.uuid4(),
            indicator_code=indicator_code,
            period_key=period_key,
            min_x=Decimal(str(min_x)),
            max_x=Decimal(str(max_x)),
            reference_population_size=reference_population_size,
            is_closed=is_closed,
            computed_at=utc_now(),
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_value(
        self, entity_key: uuid.UUID | None, country: str, indicator_code: str, period_type: str, period_key: str
    ) -> IndicatorValue | None:
        stmt = select(IndicatorValue).where(
            IndicatorValue.entity_key == entity_key,
            IndicatorValue.country == country,
            IndicatorValue.indicator_code == indicator_code,
            IndicatorValue.period_type == period_type,
            IndicatorValue.period_key == period_key,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def upsert_value(
        self,
        *,
        entity_key: uuid.UUID | None,
        country: str,
        indicator_code: str,
        period_type: str,
        period_key: str,
        source: str,
        raw_value: Decimal | float | None,
        normalized_score: Decimal | float | None,
        direction: str | None,
        calculation_window: str | None,
        availability_status: str,
        notes: str | None,
        business_context: str | None = None,
        interpretation_band: str | None = None,
    ) -> IndicatorValue:
        existing = await self.get_value(entity_key, country, indicator_code, period_type, period_key)
        raw_decimal = None if raw_value is None else Decimal(str(raw_value))
        score_decimal = None if normalized_score is None else Decimal(str(normalized_score))

        if existing is not None:
            existing.source = source
            existing.raw_value = raw_decimal
            existing.normalized_score = score_decimal
            existing.direction = direction
            existing.calculation_window = calculation_window
            existing.availability_status = availability_status
            existing.notes = notes
            existing.business_context = business_context
            existing.interpretation_band = interpretation_band
            existing.computed_at = utc_now()
            await self._session.flush()
            return existing

        row = IndicatorValue(
            id=uuid.uuid4(),
            entity_key=entity_key,
            country=country,
            indicator_code=indicator_code,
            period_type=period_type,
            period_key=period_key,
            source=source,
            raw_value=raw_decimal,
            normalized_score=score_decimal,
            direction=direction,
            calculation_window=calculation_window,
            availability_status=availability_status,
            notes=notes,
            business_context=business_context,
            interpretation_band=interpretation_band,
            computed_at=utc_now(),
        )
        self._session.add(row)
        await self._session.flush()
        return row
