"""Cap. 4 §4.4 — raw value computation for the OECD-derived MOS indicator
(docs/marketmind-capitulo4-metricas.md): Real Estate Growth only.

OECD inflation (CPI Level/Volatility) was **removed as a source 2026-07-08**
(see marketmind_mos_engine): Cap. 4 needs the series filtered by
EXPENDITURE=_T, TRANSFORMATION=GY, ADJUSTMENT=N in addition to MEASURE=CPI,
but marketmind-oecd only persists REF_AREA/INDICATOR/MEASURE/FREQ/UNIT — so
`measure='CPI'` alone averaged several unrelated rebased sub-indices
together (confirmed live: same time_period, values from 114 to 146). World
Bank's FP.CPI.TOTL.ZG (a clean annual %) is the sole inflation source now —
see worldbank.py::compute_inflation. Reinstate here only if marketmind-oecd
starts persisting the full SDMX series key.
"""

from sqlalchemy import text

from app.db.external import get_external_engine
from app.domain.enums import AvailabilityStatus, DataSource, IndicatorCode, IndicatorDirection
from app.services.indicators.results import RawIndicatorResult

_HOUSE_PRICE_WINDOW_QUARTERS = 12  # 3 years of quarterly RHP observations

# Fase 5 (POST /analyze) — which native measure code(s) must be collected to
# support each Cap. 4 indicator. RHP confirmed live against the real OECD API
# (dataflow DSD_AN_HOUSE_PRICES@DF_HOUSE_PRICES, agency OECD.ECO.MPD) — see
# marketmind-oecd _SERIES_KEY_LAYOUT.
RAW_INDICATOR_CODES: dict[IndicatorCode, list[str]] = {
    IndicatorCode.REAL_ESTATE_GROWTH_SCORE: ["RHP"],
}


async def _recent_real_house_price_series(country_code: str) -> list[float]:
    """RHP (Real House Prices) — descending by time_period, most recent
    first. Level is NOT comparable across countries (rebased to 100 per
    country at BASE_PER, confirmed live: ESP and USA both =100.00 in
    2015 — see marketmind_mos_engine); only a growth rate computed within
    the same series is meaningful, same principle as GDP Growth Score."""
    engine = get_external_engine(DataSource.OECD)
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT value FROM oecd_observations "
                "WHERE country = :country AND measure = 'RHP' AND value IS NOT NULL "
                "ORDER BY time_period DESC LIMIT :limit"
            ),
            {"country": country_code, "limit": _HOUSE_PRICE_WINDOW_QUARTERS},
        )
        return [float(row[0]) for row in result.all()]


async def compute_real_estate_growth(country_code: str) -> RawIndicatorResult:
    """Economic Readiness — Real Estate Growth Score (added 2026-07-08, see
    marketmind_mos_engine). Real House Prices (RHP) level is rebased to 100
    per country and NOT cross-country comparable, but a growth rate computed
    within the same series is (rebasing doesn't affect a ratio of two points
    on the same series) — same principle already used for GDP Growth Score.

    RealEstateGrowthRaw = % change between the average of the most recent 4
    quarters and the average of the oldest 4 quarters in a rolling ~3-year
    (12-quarter) window. Needs >= 8 quarters to form two non-overlapping
    windows; condicional (only OECD countries publish this dataflow), never
    mandatory — see dimension_mapping.py.

    Known limitation, not yet wired end-to-end: `marketmind-oecd` needs the
    `DSD_AN_HOUSE_PRICES@DF_HOUSE_PRICES` dataflow collected with
    measure='RHP' for this to return data; the series-key layout optimization
    (`_SERIES_KEY_LAYOUT` in that service) hasn't been confirmed against the
    live datastructure yet, so collection falls back to the slower "all"
    query path until that's investigated (documented, not silently skipped).
    """
    series = await _recent_real_house_price_series(country_code)
    if len(series) < 8:
        return RawIndicatorResult(
            None, IndicatorDirection.DIRECT, "last_12q", AvailabilityStatus.CONDITIONAL,
            f"oecd_observations: only {len(series)} RHP quarter(s), need >= 8 (2 windows of 4) — "
            "conditional indicator, only OECD countries publish this dataflow",
        )  # fmt: skip

    recent_window = series[:4]  # most recent 4 quarters (series is DESC)
    old_window = series[-4:]  # oldest 4 quarters within the fetched window
    recent_avg = sum(recent_window) / len(recent_window)
    old_avg = sum(old_window) / len(old_window)
    growth = ((recent_avg - old_avg) / max(abs(old_avg), 1.0)) * 100
    return RawIndicatorResult(
        growth, IndicatorDirection.DIRECT, "last_12q", AvailabilityStatus.CONDITIONAL, f"k={len(series)} quarters",
    )  # fmt: skip
