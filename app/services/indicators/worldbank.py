"""Cap. 4 §4.3 — raw value computation for the 7 World-Bank-derived MOS
indicators (docs/marketmind-capitulo4-metricas.md). Filters by
``country_iso2`` (not the WB-native ``country`` column) — the same choice
reconciliation_service already made, since ISO2 is the one code we
seed/know precisely for every reference country."""

import math

from sqlalchemy import text

from app.db.external import get_external_engine
from app.domain.enums import AvailabilityStatus, DataSource, IndicatorCode, IndicatorDirection
from app.services.indicators.results import RawIndicatorResult

_GDP_PER_CAPITA = "NY.GDP.PCAP.CD"
_INFLATION = "FP.CPI.TOTL.ZG"
_MOBILE = "IT.CEL.SETS.P2"
_INTERNET = "IT.NET.USER.ZS"
_GDP_GROWTH = "NY.GDP.MKTP.KD.ZG"
_POPULATION = "SP.POP.TOTL"
_URBANIZATION = "SP.URB.TOTL.IN.ZS"
# Macro Demand Structure (added 2026-07-08, GDP = C + I + G + NX identity —
# see marketmind_mos_engine). All 4 confirmed live against the real World
# Bank API before use (Cap. 4 §4.0 "no inventa indicadores"). Used as %GDP
# shares, not per-capita absolute levels, on purpose — a share doesn't
# duplicate the GDP-per-capita level signal already in Economic Readiness
# (same multicollinearity concern as known gap #2).
_CONSUMER_SPENDING = "NE.CON.PRVT.ZS"  # Households and NPISHs final consumption expenditure (% of GDP)
_GOVERNMENT_EXPENDITURE = "NE.CON.GOVT.ZS"  # General government final consumption expenditure (% of GDP)
_INVESTMENT = "NE.GDI.FTOT.ZS"  # Gross fixed capital formation (% of GDP) — proxy for investment/infrastructure
_NET_EXPORTS = "NE.RSB.GNFS.ZS"  # External balance on goods and services (% of GDP)
_TRADE_OPENNESS = "NE.TRD.GNFS.ZS"  # Trade (% of GDP) = (exports + imports) / GDP

# Fase 5 (POST /analyze) — which native WDI code(s) must be collected to
# support each Cap. 4 indicator, reused by both the raw-value functions
# above (as the private constants) and analysis_service.py's coverage-gap
# construction, so the two never drift apart.
RAW_INDICATOR_CODES: dict[IndicatorCode, list[str]] = {
    IndicatorCode.GDP_PER_CAPITA_SCORE: [_GDP_PER_CAPITA],
    IndicatorCode.INTERNET_PENETRATION_SCORE: [_INTERNET],
    IndicatorCode.MOBILE_CONNECTIVITY_SCORE: [_MOBILE],
    IndicatorCode.GDP_GROWTH_SCORE: [_GDP_GROWTH],
    IndicatorCode.POPULATION_SCALE_SCORE: [_POPULATION],
    IndicatorCode.URBANIZATION_SCORE: [_URBANIZATION],
    IndicatorCode.WORLDBANK_INFLATION_SCORE: [_INFLATION],
    IndicatorCode.CONSUMER_SPENDING_SCORE: [_CONSUMER_SPENDING],
    IndicatorCode.GOVERNMENT_EXPENDITURE_SCORE: [_GOVERNMENT_EXPENDITURE],
    IndicatorCode.INVESTMENT_SCORE: [_INVESTMENT],
    IndicatorCode.NET_EXPORTS_SCORE: [_NET_EXPORTS],
    IndicatorCode.TRADE_OPENNESS_SCORE: [_TRADE_OPENNESS],
}


async def _latest_value(country_iso2: str, indicator_code: str) -> float | None:
    engine = get_external_engine(DataSource.WORLDBANK)
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT value FROM worldbank_observations "
                "WHERE country_iso2 = :iso2 AND indicator_code = :code AND value IS NOT NULL "
                "ORDER BY year DESC LIMIT 1"
            ),
            {"iso2": country_iso2, "code": indicator_code},
        )
        row = result.first()
    return None if row is None else float(row[0])


async def _recent_average(country_iso2: str, indicator_code: str, k: int) -> tuple[float | None, int]:
    engine = get_external_engine(DataSource.WORLDBANK)
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT value FROM worldbank_observations "
                "WHERE country_iso2 = :iso2 AND indicator_code = :code AND value IS NOT NULL "
                "ORDER BY year DESC LIMIT :k"
            ),
            {"iso2": country_iso2, "code": indicator_code, "k": k},
        )
        values = [float(row[0]) for row in result.all()]
    if not values:
        return None, 0
    return sum(values) / len(values), len(values)


async def compute_gdp_per_capita(country_iso2: str) -> RawIndicatorResult:
    """Cap. 4 §4.3.1 — GDPpcRaw = y_latest, no currency conversion."""
    value = await _latest_value(country_iso2, _GDP_PER_CAPITA)
    if value is None:
        return RawIndicatorResult(
            None, IndicatorDirection.DIRECT, "y_latest", AvailabilityStatus.UNAVAILABLE,
            f"worldbank_observations: no {_GDP_PER_CAPITA} value (Cap. 4 §4.3.1)",
        )  # fmt: skip
    return RawIndicatorResult(value, IndicatorDirection.DIRECT, "y_latest", AvailabilityStatus.AVAILABLE, None)


async def compute_internet_penetration(country_iso2: str) -> RawIndicatorResult:
    """Cap. 4 §4.3.2 — InternetRaw = u_latest, truncated to [0,100]."""
    value = await _latest_value(country_iso2, _INTERNET)
    if value is None:
        return RawIndicatorResult(
            None, IndicatorDirection.DIRECT, "u_latest", AvailabilityStatus.UNAVAILABLE,
            f"worldbank_observations: no {_INTERNET} value (Cap. 4 §4.3.2)",
        )  # fmt: skip
    return RawIndicatorResult(
        max(0.0, min(100.0, value)), IndicatorDirection.DIRECT, "u_latest", AvailabilityStatus.AVAILABLE, None
    )


async def compute_mobile_connectivity(country_iso2: str) -> RawIndicatorResult:
    """Cap. 4 §4.3.3 — MobileRaw = m_latest, values > 100 accepted."""
    value = await _latest_value(country_iso2, _MOBILE)
    if value is None:
        return RawIndicatorResult(
            None, IndicatorDirection.DIRECT, "m_latest", AvailabilityStatus.UNAVAILABLE,
            f"worldbank_observations: no {_MOBILE} value (Cap. 4 §4.3.3)",
        )  # fmt: skip
    return RawIndicatorResult(value, IndicatorDirection.DIRECT, "m_latest", AvailabilityStatus.AVAILABLE, None)


async def compute_gdp_growth(country_iso2: str) -> RawIndicatorResult:
    """Cap. 4 §4.3.4 — GDPGrowthRaw = mean of last k=min(3, years available)."""
    avg, k = await _recent_average(country_iso2, _GDP_GROWTH, 3)
    if avg is None:
        return RawIndicatorResult(
            None, IndicatorDirection.DIRECT, "last_3y", AvailabilityStatus.UNAVAILABLE,
            f"worldbank_observations: no {_GDP_GROWTH} value (Cap. 4 §4.3.4)",
        )  # fmt: skip
    return RawIndicatorResult(avg, IndicatorDirection.DIRECT, "last_3y", AvailabilityStatus.AVAILABLE, f"k={k}")


async def compute_population_scale(country_iso2: str) -> RawIndicatorResult:
    """Cap. 4 §4.3.5 — PopulationRaw = ln(p_latest)."""
    value = await _latest_value(country_iso2, _POPULATION)
    if value is None or value <= 0:
        return RawIndicatorResult(
            None, IndicatorDirection.DIRECT, "ln(p_latest)", AvailabilityStatus.UNAVAILABLE,
            f"worldbank_observations: no {_POPULATION} value (Cap. 4 §4.3.5)",
        )  # fmt: skip
    return RawIndicatorResult(
        math.log(value), IndicatorDirection.DIRECT, "ln(p_latest)", AvailabilityStatus.AVAILABLE, None
    )


async def compute_urbanization(country_iso2: str) -> RawIndicatorResult:
    """Cap. 4 §4.3.6 — UrbanizationRaw = urb_latest, truncated to [0,100]."""
    value = await _latest_value(country_iso2, _URBANIZATION)
    if value is None:
        return RawIndicatorResult(
            None, IndicatorDirection.DIRECT, "urb_latest", AvailabilityStatus.UNAVAILABLE,
            f"worldbank_observations: no {_URBANIZATION} value (Cap. 4 §4.3.6)",
        )  # fmt: skip
    return RawIndicatorResult(
        max(0.0, min(100.0, value)), IndicatorDirection.DIRECT, "urb_latest", AvailabilityStatus.AVAILABLE, None
    )


async def compute_worldbank_inflation_fallback(country_iso2: str) -> RawIndicatorResult:
    """Cap. 4 §4.3.7 — fallback only, caller must have already confirmed
    OECD has no usable series for this country/period. availability_status
    is FALLBACK, not AVAILABLE, by construction."""
    avg, k = await _recent_average(country_iso2, _INFLATION, 3)
    if avg is None:
        return RawIndicatorResult(
            None, IndicatorDirection.INVERSE, "last_3y", AvailabilityStatus.UNAVAILABLE,
            f"worldbank_observations: no {_INFLATION} value (Cap. 4 §4.3.7)",
        )  # fmt: skip
    return RawIndicatorResult(
        avg, IndicatorDirection.INVERSE, "last_3y", AvailabilityStatus.FALLBACK,
        f"k={k}, WB fallback (OECD unavailable)",
    )  # fmt: skip


async def compute_consumer_spending(country_iso2: str) -> RawIndicatorResult:
    """Macro Demand Structure — Consumer Spending Score. C in GDP = C+I+G+NX.
    ConsumerSpendingRaw = latest household final consumption expenditure,
    % of GDP. Mayor = mejor (a bigger private-consumption share signals a
    more consumer-driven economy)."""
    value = await _latest_value(country_iso2, _CONSUMER_SPENDING)
    if value is None:
        return RawIndicatorResult(
            None, IndicatorDirection.DIRECT, "y_latest", AvailabilityStatus.UNAVAILABLE,
            f"worldbank_observations: no {_CONSUMER_SPENDING} value",
        )  # fmt: skip
    return RawIndicatorResult(value, IndicatorDirection.DIRECT, "y_latest", AvailabilityStatus.AVAILABLE, None)


async def compute_government_expenditure(country_iso2: str) -> RawIndicatorResult:
    """Context-only — Government Expenditure. G in GDP = C+I+G+NX. No
    defensible mayor/menor direction for "is this a good market to sell a
    private product in": France/Denmark (~23-24% of GDP, among the highest
    in the world) and India (~11%) show this reflects economic *model*
    (welfare state vs. developing economy), not consumer-market quality —
    confirmed against live World Bank data, see marketmind_mos_engine.
    availability_status is AVAILABLE but the caller must route this through
    NormalizationMethod.CONTEXT_ONLY (never scored, never enters a
    dimension)."""
    value = await _latest_value(country_iso2, _GOVERNMENT_EXPENDITURE)
    if value is None:
        return RawIndicatorResult(
            None, IndicatorDirection.DIRECT, "y_latest", AvailabilityStatus.UNAVAILABLE,
            f"worldbank_observations: no {_GOVERNMENT_EXPENDITURE} value",
        )  # fmt: skip
    return RawIndicatorResult(value, IndicatorDirection.DIRECT, "y_latest", AvailabilityStatus.AVAILABLE, None)


async def compute_investment(country_iso2: str) -> RawIndicatorResult:
    """Macro Demand Structure — Investment Score. I in GDP = C+I+G+NX.
    InvestmentRaw = latest gross fixed capital formation, % of GDP (proxy
    for investment/infrastructure — few countries publish "infrastructure
    investment" directly). Mayor = mejor."""
    value = await _latest_value(country_iso2, _INVESTMENT)
    if value is None:
        return RawIndicatorResult(
            None, IndicatorDirection.DIRECT, "y_latest", AvailabilityStatus.UNAVAILABLE,
            f"worldbank_observations: no {_INVESTMENT} value",
        )  # fmt: skip
    return RawIndicatorResult(value, IndicatorDirection.DIRECT, "y_latest", AvailabilityStatus.AVAILABLE, None)


async def compute_net_exports(country_iso2: str) -> RawIndicatorResult:
    """Macro Demand Structure — Net Exports Score. NX in GDP = C+I+G+NX.
    NetExportsRaw = latest external balance on goods and services, % of GDP
    (can be negative — a trade deficit). Mayor = mejor per Cap. 4's default
    convention; kept alongside Trade Openness Score (not a replacement) per
    explicit user decision 2026-07-08 — see marketmind_mos_engine."""
    value = await _latest_value(country_iso2, _NET_EXPORTS)
    if value is None:
        return RawIndicatorResult(
            None, IndicatorDirection.DIRECT, "y_latest", AvailabilityStatus.UNAVAILABLE,
            f"worldbank_observations: no {_NET_EXPORTS} value",
        )  # fmt: skip
    return RawIndicatorResult(value, IndicatorDirection.DIRECT, "y_latest", AvailabilityStatus.AVAILABLE, None)


async def compute_trade_openness(country_iso2: str) -> RawIndicatorResult:
    """Macro Demand Structure — Trade Openness Score. Not one of the 4
    C+I+G+NX terms itself, but a complementary structural signal (added
    2026-07-08 alongside Net Exports, not instead of it — see
    marketmind_mos_engine): (exports + imports) / GDP, a magnitude, not a
    share that trades off against C/I/G/NX. Mayor = mejor — a more
    globally-integrated economy is generally an easier one for a foreign
    product to enter."""
    value = await _latest_value(country_iso2, _TRADE_OPENNESS)
    if value is None:
        return RawIndicatorResult(
            None, IndicatorDirection.DIRECT, "y_latest", AvailabilityStatus.UNAVAILABLE,
            f"worldbank_observations: no {_TRADE_OPENNESS} value",
        )  # fmt: skip
    return RawIndicatorResult(value, IndicatorDirection.DIRECT, "y_latest", AvailabilityStatus.AVAILABLE, None)
