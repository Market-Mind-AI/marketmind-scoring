"""Cap. 4 §4.5 — raw value computation for the 3 UN-Data-derived MOS
indicators (docs/marketmind-capitulo4-metricas.md). Filters `country` by
ISO2 — the same code collection_client sends when dispatching UNData
collection (see resolve_source_country_code), so it matches what's stored."""

from sqlalchemy import text

from app.db.external import get_external_engine
from app.domain.enums import AvailabilityStatus, DataSource, IndicatorCode, IndicatorDirection
from app.services.indicators.results import RawIndicatorResult

_POPULATION_GROWTH = "POPULATION_GROWTH"
_NET_MIGRATION = "NET_MIGRATION"
_TOTAL_POPULATION = "TOTAL_POPULATION"
_MEDIAN_AGE = "MEDIAN_AGE"

# Fase 5 (POST /analyze) — which native indicator code(s) must be collected
# to support each Cap. 4 indicator. Net Migration Rate needs *two* separate
# collected series (NET_MIGRATION and TOTAL_POPULATION, cross-joined by year
# in compute_net_migration_rate above) — hence a list, not a single code.
RAW_INDICATOR_CODES: dict[IndicatorCode, list[str]] = {
    IndicatorCode.POPULATION_GROWTH_SCORE: [_POPULATION_GROWTH],
    IndicatorCode.NET_MIGRATION_RATE_SCORE: [_NET_MIGRATION, _TOTAL_POPULATION],
    IndicatorCode.MEDIAN_AGE_FIT_SCORE: [_MEDIAN_AGE],
}


async def _recent_average(country_iso2: str, indicator_code: str, k: int) -> tuple[float | None, int]:
    engine = get_external_engine(DataSource.UNDATA)
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT value FROM undata_observations "
                "WHERE country = :country AND indicator_code = :code AND value IS NOT NULL "
                "ORDER BY time_period DESC LIMIT :k"
            ),
            {"country": country_iso2, "code": indicator_code, "k": k},
        )
        values = [float(row[0]) for row in result.all()]
    if not values:
        return None, 0
    return sum(values) / len(values), len(values)


async def compute_population_growth(country_iso2: str) -> RawIndicatorResult:
    """Cap. 4 §4.5.1 — PopulationGrowthRaw = mean of last k=min(3, years), negative values accepted."""
    avg, k = await _recent_average(country_iso2, _POPULATION_GROWTH, 3)
    if avg is None:
        return RawIndicatorResult(
            None, IndicatorDirection.DIRECT, "last_3y", AvailabilityStatus.UNAVAILABLE,
            f"undata_observations: no {_POPULATION_GROWTH} value (Cap. 4 §4.5.1)",
        )  # fmt: skip
    return RawIndicatorResult(avg, IndicatorDirection.DIRECT, "last_3y", AvailabilityStatus.AVAILABLE, f"k={k}")


async def compute_net_migration_rate(country_iso2: str) -> RawIndicatorResult:
    """Cap. 4 §4.5.2 — rate_t = (nm_t / pop_t) * 1000, mean of last k=min(3,
    years with BOTH NET_MIGRATION and TOTAL_POPULATION for that year)."""
    engine = get_external_engine(DataSource.UNDATA)
    async with engine.connect() as connection:
        migration_result = await connection.execute(
            text(
                "SELECT time_period, value FROM undata_observations "
                "WHERE country = :country AND indicator_code = :code AND value IS NOT NULL"
            ),
            {"country": country_iso2, "code": _NET_MIGRATION},
        )
        migration_by_year = {row[0]: float(row[1]) for row in migration_result.all()}

        population_result = await connection.execute(
            text(
                "SELECT time_period, value FROM undata_observations "
                "WHERE country = :country AND indicator_code = :code AND value IS NOT NULL"
            ),
            {"country": country_iso2, "code": _TOTAL_POPULATION},
        )
        population_by_year = {row[0]: float(row[1]) for row in population_result.all()}

    common_years = sorted(set(migration_by_year) & set(population_by_year), reverse=True)[:3]
    if not common_years:
        return RawIndicatorResult(
            None, IndicatorDirection.DIRECT, "last_3y", AvailabilityStatus.UNAVAILABLE,
            "undata_observations: no year with both NET_MIGRATION and TOTAL_POPULATION (Cap. 4 §4.5.2)",
        )  # fmt: skip

    rates = [(migration_by_year[y] / population_by_year[y]) * 1000 for y in common_years if population_by_year[y] != 0]
    if not rates:
        return RawIndicatorResult(
            None, IndicatorDirection.DIRECT, "last_3y", AvailabilityStatus.UNAVAILABLE,
            "undata_observations: population is 0 for every matched year (Cap. 4 §4.5.2)",
        )  # fmt: skip

    return RawIndicatorResult(
        sum(rates) / len(rates), IndicatorDirection.DIRECT, "last_3y", AvailabilityStatus.AVAILABLE, f"k={len(rates)}"
    )


async def compute_median_age_fit(
    country_iso2: str, target_age_min: int | None, target_age_max: int | None
) -> RawIndicatorResult:
    """Cap. 4 §4.5.3 — conditional: only computed when the Product Profile
    defines a target age range. AgeFitRaw = max(0, 1 - D/max(W,1)) * 100."""
    if target_age_min is None or target_age_max is None:
        return RawIndicatorResult(
            None, IndicatorDirection.DIRECT, "n/a", AvailabilityStatus.CONDITIONAL,
            "Product Profile has no target_age_min/target_age_max (Cap. 4 §4.5.3)",
        )  # fmt: skip

    engine = get_external_engine(DataSource.UNDATA)
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT value FROM undata_observations "
                "WHERE country = :country AND indicator_code = :code AND value IS NOT NULL "
                "ORDER BY time_period DESC LIMIT 1"
            ),
            {"country": country_iso2, "code": _MEDIAN_AGE},
        )
        row = result.first()

    if row is None:
        return RawIndicatorResult(
            None, IndicatorDirection.DIRECT, "A_latest", AvailabilityStatus.UNAVAILABLE,
            f"undata_observations: no {_MEDIAN_AGE} value (Cap. 4 §4.5.3)",
        )  # fmt: skip

    median_age = float(row[0])
    center = (target_age_min + target_age_max) / 2
    half_width = (target_age_max - target_age_min) / 2
    distance = abs(median_age - center)
    age_fit_raw = max(0.0, 1 - (distance / max(half_width, 1))) * 100
    return RawIndicatorResult(
        age_fit_raw, IndicatorDirection.DIRECT, "A_latest", AvailabilityStatus.CONDITIONAL,
        f"median_age={median_age}, center={center}, half_width={half_width}",
    )  # fmt: skip
