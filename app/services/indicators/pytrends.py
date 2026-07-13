"""Cap. 4 §4.2 — raw value computation for the 5 PyTrends-derived MOS
indicators (docs/marketmind-capitulo4-metricas.md). Reads directly from
PyTrends' own database (read-only, via app/db/external.py) — same pattern as
reconciliation_service/enrichment_service, never writes there.

Trend/Growth/Demand Stability read over a rolling 6-month window ending
today (2026-07-08 fix), not the single calendar month named by period_key —
Google Trends never has data for the month still in progress (a few days'
lag plus weekly granularity means the freshest point is always in a
previous month), so filtering to exactly period_key's month structurally
always returned zero rows for a "compute this month's MOS now" request.
period_key still labels which month's MOS this is (calculation_window),
it just no longer bounds the query. Only MONTH period_key labeling is
implemented — see app/utils/period.py.
"""

import math
from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.external import get_external_engine
from app.domain.enums import AvailabilityStatus, DataSource, IndicatorDirection
from app.services.indicators.results import RawIndicatorResult
from app.utils.period import last_n_month_period_keys, month_period_to_date_range
from app.utils.time import utc_now

_TREND_WINDOW_MONTHS = 6
# Minimum initial-window mean (on Google Trends' 0-100 scale) below which a
# growth rate is treated as noise rather than signal — see compute_growth_score.
_MIN_GROWTH_BASELINE = 5.0


def _six_month_window() -> tuple[date, date]:
    period_keys = last_n_month_period_keys(_TREND_WINDOW_MONTHS)
    start = month_period_to_date_range(period_keys[-1])[0]  # oldest month in the window, most-recent-first list
    return start, utc_now().date()


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


async def best_localized_term(base_keyword: str, country_code: str, term_language: str | None, fallback: str) -> str:
    """The country's WINNING local search term = the variation with the highest
    worldwide interest_by_country value for that country (2026-07-11). The
    worldwide multi-term comparison already arbitrated which of the LLM's
    candidate names is the real one; reuse that verdict for the per-country
    series (interest_over_time / interest_by_region / related_queries) so they
    query a term that actually has search volume — the LLM's best-guess-first
    variation can be too niche to have a standalone per-country time series
    (e.g. 'presunto de porco preto' returns no series even though 'presunto
    pata negra' does). Falls back to `fallback` when no worldwide value exists."""
    engine = get_external_engine(DataSource.PYTRENDS)
    query = "SELECT term FROM interest_by_country WHERE keyword = :keyword AND country = :country"
    params: dict[str, str] = {"keyword": base_keyword, "country": country_code}
    if term_language is not None:
        query += " AND term_language = :term_language"
        params["term_language"] = term_language
    query += " ORDER BY interest_value DESC NULLS LAST LIMIT 1"
    async with engine.connect() as connection:
        result = await connection.execute(text(query), params)
        row = result.first()
    if row is None or row[0] is None:
        return fallback
    return str(row[0])


async def compute_trend_score(
    keyword: str, country_code: str, period_key: str, term_language: str | None = None
) -> RawIndicatorResult:
    """Cap. 4 §4.2.1 — cross-country demand LEVEL.

    Reads the WORLDWIDE Google Trends breakdown (interest_by_country table,
    geo='' resolution=COUNTRY collected once per product), where every
    country sits on a single global 0-100 axis representing the share of
    that country's searches devoted to the term (2026-07-10 redesign, see
    marketmind_mos_engine). This is what makes trend_score comparable across
    countries — the previous per-country interest_over_time was self-scaled
    to each country's own peak, so a term near its national peak in the US
    outscored the product's cultural home market (e.g. jamón: Spain), which
    inverted the MOS ranking. Growth and Demand Stability still read the
    per-country interest_over_time series (they're ratios/shapes, already
    comparable). country_code is the ISO-2 code, matched against the
    worldwide breakdown's per-country rows.

    Keyword localization by data arbitration (2026-07-11): the worldwide query
    compares MULTIPLE real local-name variations per language on that common
    axis, so each country is read against the terms in ITS OWN language
    (term_language = the country's search_language). The score is the MAX over
    those variations — the single winning local name that real search volume
    picked (user decision 2026-07-11, replacing SUM). MAX makes the score
    independent of how many candidates the LLM proposed per language: SUM
    inflated whoever had more/overlapping synonyms, double-counted near-
    synonyms (the same searcher counted twice), and — after clamping — tied
    every strong country at 100 (Spain and Portugal both hit 100). MAX still
    removes the dependence on the LLM naming the term exactly right — the data
    decides which candidate is the real one, MAX just reads its value.
    `keyword` is the product's canonical base keyword (the row's grouping key);
    when term_language is None (country has no search_language) the max is over
    all of the country's rows."""
    # interest_by_country accumulates rows from every collection run (no
    # upsert — each worldwide call is a new trend_request), so a term can have
    # a stale duplicate. Reduce to the freshest value per DISTINCT term (max
    # created_at) first, then take the max across terms.
    engine = get_external_engine(DataSource.PYTRENDS)
    inner = (
        "SELECT DISTINCT ON (term) interest_value AS v "
        "FROM interest_by_country WHERE keyword = :keyword AND country = :country"
    )
    params: dict[str, str] = {"keyword": keyword, "country": country_code}
    if term_language is not None:
        inner += " AND term_language = :term_language"
        params["term_language"] = term_language
    inner += " ORDER BY term, created_at DESC"
    query = f"SELECT COALESCE(MAX(v), 0), COUNT(*) FROM ({inner}) latest_per_term"
    async with engine.connect() as connection:
        result = await connection.execute(text(query), params)
        row = result.first()

    lang_note = f" (term_language='{term_language}')" if term_language is not None else ""
    if row is None or row[1] == 0:
        return RawIndicatorResult(
            raw_value=None,
            direction=IndicatorDirection.DIRECT,
            calculation_window=period_key,
            availability_status=AvailabilityStatus.UNAVAILABLE,
            notes=f"interest_by_country: no worldwide value for '{country_code}'{lang_note} (Cap. 4 §4.2.1)",
        )

    # min(100) is only a safety guard: raw Google Trends cells are already
    # 0-100, but anchor-chaining (>5 terms) can scale a value slightly past
    # 100. MAX never sums, so this rarely triggers.
    trend_raw = min(100.0, float(row[0]))
    return RawIndicatorResult(
        raw_value=trend_raw,
        direction=IndicatorDirection.DIRECT,
        calculation_window=period_key,
        availability_status=AvailabilityStatus.AVAILABLE,
        notes=f"worldwide interest-by-country (global 0-100 axis, MAX of {row[1]} local variation(s), "
        f"term_language='{term_language or 'any'}')",
    )


async def compute_growth_score(keyword: str, country_code: str, period_key: str) -> RawIndicatorResult:
    """Cap. 4 §4.2.2 — GrowthRaw = (media_final - media_inicial) / max(media_inicial, 1) * 100,
    k = min(30, n // 3), n >= 6 required, over the last 6 months (see module docstring)."""
    start, end = _six_month_window()
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

    # Low-base noise guard (known gap #4): Google Trends interest is 0-100,
    # so an initial window mean near 0 makes (final-initial)/initial explode
    # into meaningless four-digit percentages (a real run produced +1835% for
    # Portugal off an almost-flat baseline, clamped to the bound max). A
    # near-zero baseline means there's no measurable trend, so report a flat
    # 0% growth (AVAILABLE) rather than a spurious extreme — 0 normalizes to
    # a mild below-neutral score and still contributes to the Demand mean,
    # instead of injecting noise.
    if initial_avg < _MIN_GROWTH_BASELINE:
        return RawIndicatorResult(
            raw_value=0.0,
            direction=IndicatorDirection.DIRECT,
            calculation_window=period_key,
            availability_status=AvailabilityStatus.AVAILABLE,
            notes=f"initial-window mean {initial_avg:.2f} below {_MIN_GROWTH_BASELINE} — "
            "flat 0% growth (near-zero base, no measurable trend)",
        )

    growth_raw = ((final_avg - initial_avg) / initial_avg) * 100
    return RawIndicatorResult(
        raw_value=growth_raw,
        direction=IndicatorDirection.DIRECT,
        calculation_window=period_key,
        availability_status=AvailabilityStatus.AVAILABLE,
        notes=f"n={n}, k={k}",
    )


async def compute_demand_stability_score(keyword: str, country_code: str, period_key: str) -> RawIndicatorResult:
    """Cap. 4 §4.2.3 — StabilityRaw = CV = stddev / mean (population stddev),
    over the last 6 months (see module docstring)."""
    start, end = _six_month_window()
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
    """Cap. 4 §4.2.4 — RegionalDemandRaw = mean(r_j) across the country's own
    sub-regions (PyTrends interest_by_region, geo=XX), m >= 1, zero-valued
    regions kept (not excluded).

    Scope (user decision 2026-07-11): the cross-country-comparable "world
    total" is the trend_score (worldwide interest_by_country); Regional Demand
    stays a PER-COUNTRY signal — the geographic spread of interest inside the
    country — read with the country's own localized keyword (geo=XX)."""
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
