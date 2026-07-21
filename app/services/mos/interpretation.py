"""LLM context layer (added 2026-07-08, user request: "contexto para LLM em
todos os scores, métricas e MOS"). Everything here is deterministic lookup
or a fixed banding function — never LLM-generated — same principle as the
rest of the pipeline ("El LLM puede explicar el resultado, pero no calcula
ni altera ninguno de estos valores", Cap. 4 §4.0). This module produces the
raw material (a business-meaning sentence + a categorical band) that the
eventual Fase 5 explanation call reads; it does not call any LLM itself.

English chosen for all business-meaning text per explicit user decision
2026-07-08 (Cap. 4/5 prose stays Spanish; this is a separate, new field).

Addresses known gaps #10/#11 (LLM explanation needs to narrate direction,
not just the number; needs to be built from a stable, versionable source)
by giving every indicator/dimension/MOS value a fixed sentence + band
instead of leaving narration entirely to prompt-time LLM judgment.
"""

from app.domain.enums import IndicatorCode, IndicatorDimension

INDICATOR_MEANING: dict[IndicatorCode, str] = {
    IndicatorCode.TREND_SCORE: (
        "DEMAND VOLUME — the total absolute monthly online searches to buy this product in this country "
        "(the raw_value is that real count). This is market POTENTIAL / sales headroom and is normalized "
        "proportionally: 2x the searches ~ 2x the score. A high score means many people actually want this "
        "product here. IMPORTANT for the narrative: a large volume can come from (a) sheer population or "
        "(b) genuine appetite (a high share of the population wanting it) — read this together with the "
        "'penetration' figure in notes (searches per 100k inhabitants) to tell them apart. High volume AND "
        "high penetration = big AND enthusiastic market; high volume but low penetration = big only because "
        "populous; low volume but high penetration = small but strong appetite (real potential per person)."
    ),
    IndicatorCode.GROWTH_SCORE: "Is demand for this product growing or declining?",
    IndicatorCode.DEMAND_STABILITY_SCORE: "How stable (vs. volatile) is demand over time?",
    IndicatorCode.REGIONAL_DEMAND_SCORE: "Is interest spread across the country or concentrated in a few regions?",
    IndicatorCode.SEARCH_INTENT_SCORE: "Do related searches show purchase intent, or just general interest?",
    IndicatorCode.GDP_PER_CAPITA_SCORE: "How much average purchasing power does this market have?",
    IndicatorCode.INTERNET_PENETRATION_SCORE: "What share of the population can discover/buy the product online?",
    IndicatorCode.MOBILE_CONNECTIVITY_SCORE: "How connected is the population via mobile devices?",
    IndicatorCode.GDP_GROWTH_SCORE: "Is the economy expanding or contracting?",
    IndicatorCode.POPULATION_SCALE_SCORE: "How large is the addressable population?",
    IndicatorCode.URBANIZATION_SCORE: "What share of the population lives in urban areas (easier logistics/retail reach)?",  # noqa: E501
    IndicatorCode.WORLDBANK_INFLATION_SCORE: "How high is recent consumer price inflation?",
    IndicatorCode.POPULATION_GROWTH_SCORE: "Is the population growing or shrinking?",
    IndicatorCode.NET_MIGRATION_RATE_SCORE: "Is the country gaining or losing population through migration?",
    IndicatorCode.MEDIAN_AGE_FIT_SCORE: (
        "How close is the population's median age to this product's target age range?"
    ),
    IndicatorCode.CONSUMER_SPENDING_SCORE: "How much of the economy is driven by household consumption?",
    IndicatorCode.GOVERNMENT_EXPENDITURE_SCORE: (
        "How much of the economy is driven by government spending — context only, not scored: "
        "both very high shares (e.g. Nordic welfare states) and very low shares occur in strong "
        "and weak consumer markets alike, so there is no defensible market-opportunity direction."
    ),
    IndicatorCode.INVESTMENT_SCORE: (
        "How much of the economy is being reinvested in fixed capital "
        "(a proxy for infrastructure and productive investment)?"
    ),
    IndicatorCode.NET_EXPORTS_SCORE: "Is the country a net exporter or net importer of goods and services?",
    IndicatorCode.TRADE_OPENNESS_SCORE: (
        "How integrated is this economy into global trade (exports + imports as a share of GDP)? "
        "Higher openness generally means an easier regulatory/logistical path for a foreign product to enter."
    ),
    IndicatorCode.REAL_ESTATE_GROWTH_SCORE: (
        "Is the real estate market heating up or cooling down? (growth rate of real house prices — "
        "the price level itself isn't comparable across countries, since the underlying OECD index "
        "is rebased to 100 per country)"
    ),
}

DIMENSION_MEANING: dict[IndicatorDimension, str] = {
    IndicatorDimension.DEMAND_VOLUME: (
        "How MANY people actually want to buy this product here — the absolute size of real online "
        "purchase demand (total monthly searches). This is the sales-potential/market-size signal and the "
        "highest-priority dimension: a market with far more interested buyers should rank above one with "
        "better economic conditions but little demand. Cross-reference the per-country absolute numbers and "
        "penetration (searches per 100k inhabitants) to explain WHETHER the volume comes from population size "
        "or from genuine appetite."
    ),
    IndicatorDimension.DEMAND_QUALITY: (
        "How GOOD that demand is, independent of market size — is it growing, stable over time, and "
        "purchase-oriented (people searching to buy) rather than merely curious? A small market can score "
        "very high here: strong quality flags real potential even when the absolute volume is modest."
    ),
    IndicatorDimension.MACRO_DEMAND_STRUCTURE: (
        "The country's aggregate demand composition (GDP = Consumption + Investment + Government "
        "spending + Net exports) — how much of the economy is driven by private consumption, "
        "investment, and trade integration."
    ),
    IndicatorDimension.MARKET_SIZE: "The scale of the addressable audience in this country.",
    IndicatorDimension.ECONOMIC_READINESS: ("The market's purchasing power, economic momentum, and price stability."),
    IndicatorDimension.DIGITAL_READINESS: "Whether the population can discover and purchase the product online.",
    IndicatorDimension.DEMOGRAPHIC_SUSTAINABILITY: (
        'Long-term population trends — less tied to "will this sell now" than the other dimensions.'
    ),
}

# Generic 5-level band for any 0-100 normalized score (indicator or
# dimension sub-score) — standardizes the interpretation bands Trend Score
# already documented in prose (Cap. 4 §4.2.1: "0-20 muy baja, 20-40 baja,
# 40-60 moderada, 60-80 alta, 80-100 muy alta") into a reusable function.
_SCORE_BANDS: list[tuple[float, str]] = [
    (20.0, "Very Low"),
    (40.0, "Low"),
    (60.0, "Moderate"),
    (80.0, "High"),
]


def score_band(score: float) -> str:
    """0-100 normalized score -> Very Low/Low/Moderate/High/Very High."""
    for upper_bound, label in _SCORE_BANDS:
        if score < upper_bound:
            return label
    return "Very High"


# MOS interpretation table, Cap. 5 §5.8 — codified here instead of only
# living as prose, so it can be attached to the mos_results payload.
_MOS_BANDS: list[tuple[float, str]] = [
    (20.0, "Very Low Opportunity"),
    (40.0, "Low Opportunity"),
    (60.0, "Moderate Opportunity"),
    (75.0, "Good Opportunity"),
    (90.0, "High Opportunity"),
]


def mos_band(mos: float) -> str:
    """0-100 MOS -> Cap. 5 §5.8 interpretation label."""
    for upper_bound, label in _MOS_BANDS:
        if mos < upper_bound:
            return label
    return "Excellent Opportunity"
