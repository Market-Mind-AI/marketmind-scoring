"""Cap. 4 §4.4 — raw value computation for the 2 OECD-derived MOS indicators
(docs/marketmind-capitulo4-metricas.md).

**Known limitation, documented on purpose (not silently guessed):** Cap. 4
specifies filtering the SDMX series by EXPENDITURE=_T, TRANSFORMATION=GY,
ADJUSTMENT=N in addition to MEASURE=CPI — but `marketmind-oecd`'s own
ingestion (app/utils/sdmx.py `_parse_series`) only extracts REF_AREA,
INDICATOR/SUBJECT, MEASURE, FREQ and UNIT into named columns; those three
extra dimensions aren't persisted anywhere queryable in `oecd_observations`
(confirmed by reading that service's code, not assumed). Filtering here is
therefore `country` + `measure='CPI'` only, relying on the collection
request itself having been scoped correctly when it was dispatched — see
app/services/collection_client.py. Revisit if/when marketmind-oecd starts
persisting the full SDMX series key.
"""

import statistics

from sqlalchemy import text

from app.db.external import get_external_engine
from app.domain.enums import AvailabilityStatus, DataSource, IndicatorDirection
from app.services.indicators.results import RawIndicatorResult

_MAX_MONTHS = 12


async def _recent_cpi_series(country_code: str) -> list[float]:
    engine = get_external_engine(DataSource.OECD)
    async with engine.connect() as connection:
        # Prefer monthly frequency (Cap. 4: "preferir mensual cuando exista"); fall back to any frequency.
        for frequency_filter in ("AND frequency = 'M'", ""):
            result = await connection.execute(
                text(
                    "SELECT value FROM oecd_observations "
                    f"WHERE country = :country AND measure = 'CPI' AND value IS NOT NULL {frequency_filter} "
                    "ORDER BY time_period DESC LIMIT :limit"
                ),
                {"country": country_code, "limit": _MAX_MONTHS},
            )
            values = [float(row[0]) for row in result.all()]
            if values:
                return values
    return []


async def compute_oecd_inflation_level(country_code: str) -> RawIndicatorResult:
    """Cap. 4 §4.4.1 — InflationLevelRaw = mean of last k=min(12, observations)."""
    series = await _recent_cpi_series(country_code)
    if not series:
        return RawIndicatorResult(
            None, IndicatorDirection.INVERSE, "last_12m", AvailabilityStatus.UNAVAILABLE,
            "oecd_observations: no CPI value for this country (Cap. 4 §4.4.1)",
        )  # fmt: skip
    return RawIndicatorResult(
        sum(series) / len(series), IndicatorDirection.INVERSE, "last_12m", AvailabilityStatus.AVAILABLE,
        f"k={len(series)}",
    )  # fmt: skip


async def compute_oecd_inflation_volatility(country_code: str) -> RawIndicatorResult:
    """Cap. 4 §4.4.2 — InflationVolatilityRaw = stddev of the same series
    used for the Inflation Level Score."""
    series = await _recent_cpi_series(country_code)
    if len(series) < 2:
        return RawIndicatorResult(
            None, IndicatorDirection.INVERSE, "last_12m", AvailabilityStatus.UNAVAILABLE,
            f"oecd_observations: only {len(series)} CPI value(s), need >= 2 for stddev (Cap. 4 §4.4.2)",
        )  # fmt: skip
    return RawIndicatorResult(
        statistics.pstdev(series), IndicatorDirection.INVERSE, "last_12m", AvailabilityStatus.AVAILABLE,
        f"k={len(series)}",
    )  # fmt: skip
