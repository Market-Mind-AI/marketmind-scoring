"""Cap. 4 §4.2 — raw value computation for the 5 PyTrends-derived MOS
indicators (docs/marketmind-capitulo4-metricas.md). Reads directly from
PyTrends' own database (read-only, via app/db/external.py) — same pattern as
reconciliation_service/enrichment_service, never writes there.

Only MONTH period slicing is implemented — see app/utils/period.py.
"""

import math
from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.external import get_external_engine
from app.domain.enums import AvailabilityStatus, DataSource, IndicatorDirection
from app.services.indicators.results import RawIndicatorResult
from app.utils.period import month_period_to_date_range

_TRANSACTIONAL_TERMS = ["amazon", "lidl", "comprar", "compra", "buy", "price", "precio", "oferta", "deal"]
_EVALUATION_TERMS = ["best", "mejor", "review", "opiniones", "comparativa", "vs", "top"]
_USAGE_TERMS = [
    "how to", "como", "cómo", "recetas", "receta", "pollo", "patatas", "boniato", "huevos", "alitas", "salmon", "use",
]  # fmt: skip


async def _fetch_interest_series(
    connection: AsyncConnection, keyword: str, country_code: str, start: date, end: date
) -> list[float]:
    result = await connection.execute(
        text(
            "SELECT interest_value FROM interest_over_time "
            "WHERE keyword = :keyword AND country = :country AND is_partial = false "
            "AND date >= :start AND date <= :end ORDER BY date ASC"
        ),
        {"keyword": keyword, "country": country_code, "start": start, "end": end},
    )
    return [float(row[0]) for row in result.all() if row[0] is not None]


async def compute_trend_score(keyword: str, country_code: str, period_key: str) -> RawIndicatorResult:
    """Cap. 4 §4.2.1 — TrendRaw = mean(v_i), n >= 2 required."""
    start, end = month_period_to_date_range(period_key)
    engine = get_external_engine(DataSource.PYTRENDS)
    async with engine.connect() as connection:
        series = await _fetch_interest_series(connection, keyword, country_code, start, end)

    if len(series) < 2:
        return RawIndicatorResult(
            raw_value=None,
            direction=IndicatorDirection.DIRECT,
            calculation_window=period_key,
            availability_status=AvailabilityStatus.UNAVAILABLE,
            notes=f"interest_over_time: only {len(series)} valid observation(s), need >= 2 (Cap. 4 §4.2.1)",
        )

    trend_raw = sum(series) / len(series)
    return RawIndicatorResult(
        raw_value=trend_raw,
        direction=IndicatorDirection.DIRECT,
        calculation_window=period_key,
        availability_status=AvailabilityStatus.AVAILABLE,
        notes=f"n={len(series)}",
    )


async def compute_growth_score(keyword: str, country_code: str, period_key: str) -> RawIndicatorResult:
    """Cap. 4 §4.2.2 — GrowthRaw = (media_final - media_inicial) / max(media_inicial, 1) * 100,
    k = min(30, n // 3), n >= 6 required."""
    start, end = month_period_to_date_range(period_key)
    engine = get_external_engine(DataSource.PYTRENDS)
    async with engine.connect() as connection:
        series = await _fetch_interest_series(connection, keyword, country_code, start, end)

    n = len(series)
    if n < 6:
        return RawIndicatorResult(
            raw_value=None,
            direction=IndicatorDirection.DIRECT,
            calculation_window=period_key,
            availability_status=AvailabilityStatus.UNAVAILABLE,
            notes=f"interest_over_time: only {n} valid observation(s), need >= 6 (Cap. 4 §4.2.2)",
        )

    k = min(30, n // 3)
    initial_avg = sum(series[:k]) / k
    final_avg = sum(series[n - k :]) / k
    growth_raw = ((final_avg - initial_avg) / max(initial_avg, 1)) * 100
    return RawIndicatorResult(
        raw_value=growth_raw,
        direction=IndicatorDirection.DIRECT,
        calculation_window=period_key,
        availability_status=AvailabilityStatus.AVAILABLE,
        notes=f"n={n}, k={k}",
    )


async def compute_demand_stability_score(keyword: str, country_code: str, period_key: str) -> RawIndicatorResult:
    """Cap. 4 §4.2.3 — StabilityRaw = CV = stddev / mean (population stddev)."""
    start, end = month_period_to_date_range(period_key)
    engine = get_external_engine(DataSource.PYTRENDS)
    async with engine.connect() as connection:
        series = await _fetch_interest_series(connection, keyword, country_code, start, end)

    if len(series) < 2:
        return RawIndicatorResult(
            raw_value=None,
            direction=IndicatorDirection.INVERSE,
            calculation_window=period_key,
            availability_status=AvailabilityStatus.UNAVAILABLE,
            notes=f"interest_over_time: only {len(series)} valid observation(s) (Cap. 4 §4.2.3)",
        )

    mean = sum(series) / len(series)
    if mean == 0:
        return RawIndicatorResult(
            raw_value=None,
            direction=IndicatorDirection.INVERSE,
            calculation_window=period_key,
            availability_status=AvailabilityStatus.UNAVAILABLE,
            notes="mean interest is 0, CV undefined (Cap. 4 §4.2.3)",
        )

    variance = sum((v - mean) ** 2 for v in series) / len(series)
    stddev = math.sqrt(variance)
    cv = stddev / mean
    return RawIndicatorResult(
        raw_value=cv,
        direction=IndicatorDirection.INVERSE,
        calculation_window=period_key,
        availability_status=AvailabilityStatus.AVAILABLE,
        notes=f"n={len(series)}, mean={mean:.2f}, stddev={stddev:.2f}",
    )


async def compute_regional_demand_score(keyword: str, country_code: str, period_key: str) -> RawIndicatorResult:
    """Cap. 4 §4.2.4 — RegionalDemandRaw = mean(r_j) across regions, m >= 1,
    zero-valued regions kept (not excluded)."""
    engine = get_external_engine(DataSource.PYTRENDS)
    async with engine.connect() as connection:
        result = await connection.execute(
            text("SELECT interest_value FROM interest_by_region WHERE keyword = :keyword AND country = :country"),
            {"keyword": keyword, "country": country_code},
        )
        values = [float(row[0]) for row in result.all() if row[0] is not None]

    if not values:
        return RawIndicatorResult(
            raw_value=None,
            direction=IndicatorDirection.DIRECT,
            calculation_window=period_key,
            availability_status=AvailabilityStatus.UNAVAILABLE,
            notes="interest_by_region: no regions with a valid value (Cap. 4 §4.2.4)",
        )

    regional_raw = sum(values) / len(values)
    return RawIndicatorResult(
        raw_value=regional_raw,
        direction=IndicatorDirection.DIRECT,
        calculation_window=period_key,
        availability_status=AvailabilityStatus.AVAILABLE,
        notes=f"m={len(values)} regions",
    )


def _classify_query_intent(query: str) -> float:
    lowered = query.lower()
    if any(term in lowered for term in _TRANSACTIONAL_TERMS):
        return 1.0
    if any(term in lowered for term in _EVALUATION_TERMS):
        return 0.8
    if any(term in lowered for term in _USAGE_TERMS):
        return 0.4
    # Cap. 4 §4.2.6 gives no lexical rule to distinguish "0.6 marca/modelo/
    # variante" from "0.0 irrelevante" — documented assumption: a query that
    # matches none of the 3 specific rules is treated as a product variant
    # (0.6), since it came from related_queries for our own keyword.
    return 0.6


async def compute_search_intent_score(keyword: str, period_key: str) -> RawIndicatorResult:
    """Cap. 4 §4.2.6 — IntentRaw = weighted average of a lexical intent
    classification over related_queries.top (never .rising — different
    scale). related_queries has no country column in the raw schema, so
    this is scoped by keyword only."""
    engine = get_external_engine(DataSource.PYTRENDS)
    async with engine.connect() as connection:
        result = await connection.execute(
            text("SELECT query, value FROM related_queries WHERE keyword = :keyword AND query_type = 'top'"),
            {"keyword": keyword},
        )
        rows = [(row[0], float(row[1])) for row in result.all() if row[0] is not None and row[1] is not None]

    if not rows:
        return RawIndicatorResult(
            raw_value=None,
            direction=IndicatorDirection.DIRECT,
            calculation_window=period_key,
            availability_status=AvailabilityStatus.UNAVAILABLE,
            notes="related_queries.top: no valid queries (Cap. 4 §4.2.6)",
        )

    total_weight = sum(w for _, w in rows)
    if total_weight == 0:
        return RawIndicatorResult(
            raw_value=None,
            direction=IndicatorDirection.DIRECT,
            calculation_window=period_key,
            availability_status=AvailabilityStatus.UNAVAILABLE,
            notes="related_queries.top: all values are 0 (Cap. 4 §4.2.6)",
        )

    weighted_sum = sum(w * _classify_query_intent(q) for q, w in rows)
    intent_raw = (weighted_sum / total_weight) * 100
    return RawIndicatorResult(
        raw_value=intent_raw,
        direction=IndicatorDirection.DIRECT,
        calculation_window=period_key,
        availability_status=AvailabilityStatus.AVAILABLE,
        notes=f"{len(rows)} queries classified",
    )
