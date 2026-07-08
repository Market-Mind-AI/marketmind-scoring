from enum import StrEnum


class DataSource(StrEnum):
    PYTRENDS = "pytrends"
    WORLDBANK = "worldbank"
    OECD = "oecd"
    UNDATA = "undata"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class JobTaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"  # already covered, no collection needed


class IngestionRunStatus(StrEnum):
    """Normalized view of the 4 services' ingestion run status, since UNData
    uses a wider set of raw values (pending/running/success/partial/failed)
    than the other 3 (running/success/failed) — see collection_client. A
    ``partial`` raw status is normalized to FAILED here: per the "sem
    imputação" principle, a run that didn't fully succeed does not count as
    coverage, even if some rows landed."""

    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class PeriodType(StrEnum):
    MONTH = "month"
    QUARTER = "quarter"
    SEMESTER = "semester"
    YEAR = "year"
    CUSTOM = "custom"


class AvailabilityStatus(StrEnum):
    """Cap. 4 §4.8 contrato funcional — availability_status."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    FALLBACK = "fallback"
    CONDITIONAL = "conditional"


class IndicatorDirection(StrEnum):
    """Cap. 4 §4.8 contrato funcional — direction."""

    DIRECT = "direct"
    INVERSE = "inverse"


class IndicatorCode(StrEnum):
    """Cap. 4 — os indicadores atômicos: 20 elegíveis pro MOS (score
    normalizado, entram em alguma dimensão) + 2 condicionais (Median Age Fit,
    Real Estate Growth) + 1 apenas-contexto (Government Expenditure, nunca
    pontuado — ver marketmind_mos_engine, seção "Government Expenditure").
    Regional Spread Insight e Rising Query Momentum (Cap. 4 §4.2.5/4.2.7)
    ficam de fora de propósito — o próprio capítulo marca "no entra al MOS
    v1", são insight de narrativa pra Fase 5."""

    TREND_SCORE = "trend_score"
    GROWTH_SCORE = "growth_score"
    DEMAND_STABILITY_SCORE = "demand_stability_score"
    REGIONAL_DEMAND_SCORE = "regional_demand_score"
    SEARCH_INTENT_SCORE = "search_intent_score"
    GDP_PER_CAPITA_SCORE = "gdp_per_capita_score"
    INTERNET_PENETRATION_SCORE = "internet_penetration_score"
    MOBILE_CONNECTIVITY_SCORE = "mobile_connectivity_score"
    GDP_GROWTH_SCORE = "gdp_growth_score"
    POPULATION_SCALE_SCORE = "population_scale_score"
    URBANIZATION_SCORE = "urbanization_score"
    OECD_INFLATION_LEVEL_SCORE = "oecd_inflation_level_score"
    OECD_INFLATION_VOLATILITY_SCORE = "oecd_inflation_volatility_score"
    WORLDBANK_INFLATION_SCORE = "worldbank_inflation_score"
    POPULATION_GROWTH_SCORE = "population_growth_score"
    NET_MIGRATION_RATE_SCORE = "net_migration_rate_score"
    MEDIAN_AGE_FIT_SCORE = "median_age_fit_score"
    # Macro Demand Structure (added 2026-07-08, GDP = C + I + G + NX identity)
    CONSUMER_SPENDING_SCORE = "consumer_spending_score"
    INVESTMENT_SCORE = "investment_score"
    NET_EXPORTS_SCORE = "net_exports_score"
    TRADE_OPENNESS_SCORE = "trade_openness_score"
    GOVERNMENT_EXPENDITURE_SCORE = "government_expenditure_score"  # context-only, never scored
    # Economic Readiness addition (added 2026-07-08)
    REAL_ESTATE_GROWTH_SCORE = "real_estate_growth_score"


class NormalizationMethod(StrEnum):
    """Como o normalized_score é derivado do raw_value — ver Cap. 4 §4.1 e a
    correção da seção 7 do plano (bounds contra população de referência fixa,
    não por consulta)."""

    # já é 0-100 (Trend, Regional Demand, Search Intent, Internet%, Urbanização%, Median Age Fit)
    PASSTHROUGH = "passthrough"
    # Min-Max contra normalization_reference (população de referência, p5/p95)
    MINMAX_REFERENCE = "minmax_reference"
    MINMAX_FIXED = "minmax_fixed"  # Min-Max contra bounds teóricos fixos (Growth Score, Demand Stability/CV)
    # Sem score — raw_value fica só como contexto pro LLM, nunca normalizado
    # nem usado em dimension_mapping/TOPSIS (Government Expenditure Score:
    # sem direção maior/menor defensável, ver marketmind_mos_engine).
    CONTEXT_ONLY = "context_only"


class IndicatorDimension(StrEnum):
    """Agrupamento hierárquico usado na agregação em 2 estágios do MOS (Cap. 5, ver marketmind_mos_engine)."""

    DEMAND = "demand"
    DIGITAL_READINESS = "digital_readiness"
    ECONOMIC_READINESS = "economic_readiness"
    MARKET_SIZE = "market_size"
    DEMOGRAPHIC_SUSTAINABILITY = "demographic_sustainability"
    MACRO_DEMAND_STRUCTURE = "macro_demand_structure"
