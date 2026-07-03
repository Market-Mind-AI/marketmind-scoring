"""Cap. 4 §4.3 — raw value computation for the 7 World-Bank-derived MOS
indicators (docs/marketmind-capitulo4-metricas.md). Filters by
``country_iso2`` (not the WB-native ``country`` column) — the same choice
reconciliation_service already made, since ISO2 is the one code we
seed/know precisely for every reference country."""

import math

from sqlalchemy import text

from app.db.external import get_external_engine
from app.domain.enums import AvailabilityStatus, DataSource, IndicatorDirection
from app.services.indicators.results import RawIndicatorResult

_GDP_PER_CAPITA = "NY.GDP.PCAP.CD"
_INFLATION = "FP.CPI.TOTL.ZG"
_MOBILE = "IT.CEL.SETS.P2"
_INTERNET = "IT.NET.USER.ZS"
_GDP_GROWTH = "NY.GDP.MKTP.KD.ZG"
_POPULATION = "SP.POP.TOTL"
_URBANIZATION = "SP.URB.TOTL.IN.ZS"


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
