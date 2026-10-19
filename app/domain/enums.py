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
    YEAR = "year"


class AvailabilityStatus(StrEnum):
    """Cap. 4 §4.8 contrato funcional — availability_status."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    CONDITIONAL = "conditional"


class IndicatorDirection(StrEnum):
    """Cap. 4 §4.8 contrato funcional — direction."""

    DIRECT = "direct"
    INVERSE = "inverse"


class IndicatorCode(StrEnum):
    """Cap. 4 — os indicadores atômicos: 18 elegíveis pro MOS (score
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
    BROADBAND_ADOPTION_SCORE = "broadband_adoption_score"
    GDP_GROWTH_SCORE = "gdp_growth_score"
    POPULATION_SCALE_SCORE = "population_scale_score"
    URBANIZATION_SCORE = "urbanization_score"
    # OECD Inflation Level/Volatility removed 2026-07-08 — marketmind-oecd
    # doesn't persist the EXPENDITURE/TRANSFORMATION/ADJUSTMENT dimensions
    # Cap. 4 needs to isolate a clean annual-growth CPI series, so `measure=
    # 'CPI'` alone was averaging several unrelated rebased sub-indices
    # together (confirmed live: same time_period, wildly different values).
    # World Bank's FP.CPI.TOTL.ZG is the sole inflation source now — see
    # WORLDBANK_INFLATION_SCORE and marketmind_mos_engine.
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
    # Volume de busca da CATEGORIA genérica do produto (2026-08-27). Contexto
    # puro, nunca pontuado: existe para a narrativa poder dizer "a marca é fraca
    # aqui, mas a busca pela categoria é altíssima". Pontuá-lo entraria em
    # conflito com o Demand Volume da própria marca e quebraria a
    # comparabilidade do Cap. 5 (decisão D2).
    CATEGORY_VOLUME_SCORE = "category_volume_score"  # context-only, never scored
    # Economic Readiness addition (added 2026-07-08)
    REAL_ESTATE_GROWTH_SCORE = "real_estate_growth_score"


class NormalizationMethod(StrEnum):
    """Como o normalized_score é derivado do raw_value — ver Cap. 4 §4.1.

    MINMAX_REFERENCE (percentil p5/p95 sobre uma cesta de países de
    referência) foi removido em 2026-07-08 — decisão do usuário: o produto
    só coleta dado dos países que o usuário efetivamente pedir em cada
    /analyze, então a cesta de referência nunca se populava organicamente, e
    qualquer normalização que dependesse dela estaria implicitamente
    comparando o país da consulta contra outros países. Todo indicador hoje
    é PASSTHROUGH, MINMAX_FIXED ou CONTEXT_ONLY — ver marketmind_mos_engine."""

    # já é 0-100 (Trend, Regional Demand, Search Intent, Internet%, Urbanização%, Median Age Fit)
    PASSTHROUGH = "passthrough"
    # Min-Max contra bounds absolutos fixos por indicador (normalization_reference,
    # period_key="FIXED") — auto-amplia quando um valor real observado cai fora do
    # range atual (nunca aperta), mas nunca compara contra outro país da mesma consulta.
    MINMAX_FIXED = "minmax_fixed"
    # Sem score — raw_value fica só como contexto pro LLM, nunca normalizado
    # nem usado em dimension_mapping/TOPSIS (Government Expenditure Score:
    # sem direção maior/menor defensável, ver marketmind_mos_engine).
    CONTEXT_ONLY = "context_only"


class IndicatorDimension(StrEnum):
    """Agrupamento hierárquico usado na agregação em 2 estágios do MOS (Cap. 5, ver marketmind_mos_engine)."""

    # Demanda dividida em duas dimensões (2026-07-16): VOLUME (tamanho absoluto
    # de mercado / potencial de vendas, nível de busca linear) e QUALITY
    # (crescimento/estabilidade/intenção — sinais de forma, independentes de
    # tamanho). Separadas porque a média das duas afogava o volume: um mercado
    # pequeno de alta qualidade empatava um mercado enorme. Ver marketmind_mos_engine.
    DEMAND_VOLUME = "demand_volume"
    DEMAND_QUALITY = "demand_quality"
    DIGITAL_READINESS = "digital_readiness"
    ECONOMIC_READINESS = "economic_readiness"
    MARKET_SIZE = "market_size"
    DEMOGRAPHIC_SUSTAINABILITY = "demographic_sustainability"
    MACRO_DEMAND_STRUCTURE = "macro_demand_structure"
