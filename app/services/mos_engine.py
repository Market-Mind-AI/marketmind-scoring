"""Fase 4: Cap. 5 orchestration — for each requested country, builds the
product's dimension vector (product-specific Demand indicators merged with
country-level macro indicators, entity_key=<product> + entity_key=NULL),
validates mandatory-indicator availability (Cap. 5 §5.3), runs TOPSIS
against the fixed ideal=100/anti-ideal=0 (Cap. 5 §5.6), and upserts one
mos_results row per country (no non-compensatory cap — user decision 2026-07-15).
Never combines countries into a single score — MOS is always absolute and
independent per (product, country, period), see memory marketmind_mos_engine.

Also computes, side by side, the classical (textbook) TOPSIS score via
pymcdm — a validation artifact only, never used to decide the product's
actual MOS (see app/services/mos/library_reference.py).
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import EntityNotFoundError
from app.core.logging import logger
from app.db.models.mos_result import MosResult
from app.domain.enums import AvailabilityStatus, IndicatorDimension, PeriodType
from app.repositories.country_repository import CountryRepository
from app.repositories.indicator_repository import IndicatorRepository
from app.repositories.mos_repository import MosRepository
from app.repositories.product_repository import ProductRepository
from app.services.mos.country_vectors import CountryVectorBuilder
from app.services.mos.dimension_mapping import check_availability, compute_dimension_subscores
from app.services.mos.interpretation import mos_band
from app.services.mos.library_reference import classical_topsis_scores
from app.services.mos.topsis import topsis_score
from app.utils.period import current_month_period_key

_ALL_DIMENSIONS = list(IndicatorDimension)


class MosEngine:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._indicator_repo = IndicatorRepository(session)
        self._mos_repo = MosRepository(session)
        self._product_repo = ProductRepository(session)
        self._country_repo = CountryRepository(session)
        self._vector_builder = CountryVectorBuilder(session)

    async def compute_mos(
        self, *, entity_key: uuid.UUID, countries: list[str], period_key: str | None = None
    ) -> list[MosResult]:
        entity = await self._product_repo.get_by_entity_key(entity_key)
        if entity is None:
            raise EntityNotFoundError(f"Entity {entity_key} not found")

        resolved_period_key = period_key or current_month_period_key()

        results = [await self._compute_one(entity_key, country_iso3, resolved_period_key) for country_iso3 in countries]
        await self._session.commit()
        return results

    async def find_incomplete_countries(
        self, *, entity_key: uuid.UUID, countries: list[str], period_key: str
    ) -> dict[str, str]:
        """Completeness gate (user principle 2026-07-11): the MOS must never be
        computed on partial data. Returns {country: reason} for every requested
        country still missing a mandatory indicator, so the caller can abort the
        whole analysis rather than emit a partial/asymmetric result. Empty dict
        means every country carries the full, uniform indicator set."""
        incomplete: dict[str, str] = {}
        for country_iso3 in countries:
            country_values = await self._indicator_repo.get_values_for(
                entity_key=None, country=country_iso3, period_type=PeriodType.MONTH.value, period_key=period_key
            )
            product_values = await self._indicator_repo.get_values_for(
                entity_key=entity_key, country=country_iso3, period_type=PeriodType.MONTH.value, period_key=period_key
            )
            reason = check_availability({**country_values, **product_values})
            if reason is not None:
                incomplete[country_iso3] = reason
        return incomplete

    async def _exclude(self, *, entity_key: uuid.UUID, country_iso3: str, period_key: str, reason: str) -> MosResult:
        return await self._mos_repo.upsert_mos_result(
            entity_key=entity_key,
            country=country_iso3,
            period_type=PeriodType.MONTH.value,
            period_key=period_key,
            mos=None,
            mos_before_cap=None,
            capped_by=None,
            topsis_score=None,
            availability_status=AvailabilityStatus.UNAVAILABLE.value,
            excluded_reason=reason,
        )

    async def _compute_one(self, entity_key: uuid.UUID, country_iso3: str, period_key: str) -> MosResult:
        country_values = await self._indicator_repo.get_values_for(
            entity_key=None, country=country_iso3, period_type=PeriodType.MONTH.value, period_key=period_key
        )
        product_values = await self._indicator_repo.get_values_for(
            entity_key=entity_key, country=country_iso3, period_type=PeriodType.MONTH.value, period_key=period_key
        )
        merged = {**country_values, **product_values}

        exclusion_reason = check_availability(merged)
        if exclusion_reason is not None:
            return await self._exclude(
                entity_key=entity_key, country_iso3=country_iso3, period_key=period_key, reason=exclusion_reason
            )

        subscores = compute_dimension_subscores(merged)
        missing_dimensions = [d.value for d in _ALL_DIMENSIONS if d not in subscores]
        if missing_dimensions:
            return await self._exclude(
                entity_key=entity_key,
                country_iso3=country_iso3,
                period_key=period_key,
                reason="Missing dimension sub-score(s): " + ", ".join(missing_dimensions),
            )

        weights: dict[IndicatorDimension, float] = {}
        for dimension in _ALL_DIMENSIONS:
            weight_row = await self._mos_repo.get_weight(dimension.value, period_key)
            if weight_row is None:
                return await self._exclude(
                    entity_key=entity_key,
                    country_iso3=country_iso3,
                    period_key=period_key,
                    reason=(
                        f"No calibrated weight for dimension={dimension.value}, period_key={period_key} "
                        "(run POST /admin/mos/refresh-weights first)"
                    ),
                )
            weights[dimension] = float(weight_row.weight)

        topsis = topsis_score(subscores, weights)
        # No non-compensatory cap (user decision 2026-07-15): the MOS is the
        # TOPSIS score directly. Demand is one weighted dimension like the
        # others; it is not allowed to hard-cap the final score.
        mos = topsis * 100
        mos_before_cap = mos
        capped_by = None

        library_topsis, library_mos = await self._classical_comparison(country_iso3, period_key, subscores)

        return await self._mos_repo.upsert_mos_result(
            entity_key=entity_key,
            country=country_iso3,
            period_type=PeriodType.MONTH.value,
            period_key=period_key,
            mos=mos,
            mos_before_cap=mos_before_cap,
            capped_by=capped_by,
            topsis_score=topsis,
            library_topsis_score=library_topsis,
            library_mos=library_mos,
            mos_interpretation=mos_band(mos),
            availability_status=AvailabilityStatus.AVAILABLE.value,
            excluded_reason=None,
        )

    async def _classical_comparison(
        self, country_iso3: str, period_key: str, target_subscores: dict[IndicatorDimension, float]
    ) -> tuple[float | None, float | None]:
        """Validation-only: classical (textbook) TOPSIS score for this same
        country/period, using pymcdm's relative ideal/anti-ideal derived from
        the reference-country population. Never raises — any failure just
        means no comparison data this time (never blocks the real MOS)."""
        countries = await self._country_repo.list_all()
        rows: list[list[float]] = []
        target_index: int | None = None
        for country in countries:
            if country.iso3 == country_iso3:
                continue
            vector = await self._vector_builder.full_vector(country.iso3, period_key)
            if all(d in vector for d in _ALL_DIMENSIONS):
                rows.append([vector[d] for d in _ALL_DIMENSIONS])

        target_index = len(rows)
        rows.append([target_subscores[d] for d in _ALL_DIMENSIONS])

        if len(rows) < 2:
            logger.warning(
                f"Not enough countries with complete data to run the classical TOPSIS comparison for {country_iso3}"
            )
            return None, None

        try:
            scores = classical_topsis_scores(rows)
        except ValueError as exc:
            logger.warning(f"Classical TOPSIS comparison skipped for {country_iso3}: {exc}")
            return None, None

        library_topsis = scores[target_index]
        return library_topsis, library_topsis * 100
