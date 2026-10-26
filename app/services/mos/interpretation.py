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
        "(the raw_value is that real count). This is market POTENTIAL / sales headroom, normalized against "
        "the PRODUCT'S REFERENCE MARKET (its highest-demand country) as the 100-point ceiling: the score is "
        "this country's demand as a percentage of the product's biggest national market, so a niche product "
        "is measured on its own scale instead of a fixed global one, and 2x the searches ~ 2x the score. "
        "IMPORTANT for the narrative: a large volume can come from (a) sheer population or (b) genuine "
        "appetite (a high share of the population wanting it) — read this together with the 'penetration' "
        "figure in notes (searches per 100k inhabitants) to tell them apart. High volume AND high penetration "
        "= big AND enthusiastic market; high volume but low penetration = big only because populous; low "
        "volume but high penetration = small but strong appetite (real potential per person)."
    ),
    IndicatorCode.GROWTH_SCORE: "Is demand for this product growing or declining?",
    IndicatorCode.DEMAND_STABILITY_SCORE: "How stable (vs. volatile) is demand over time?",
    IndicatorCode.REGIONAL_DEMAND_SCORE: "Is interest spread across the country or concentrated in a few regions?",
    IndicatorCode.SEARCH_INTENT_SCORE: "Do related searches show purchase intent, or just general interest?",
    IndicatorCode.GDP_PER_CAPITA_SCORE: "How much average purchasing power does this market have?",
    IndicatorCode.INTERNET_PENETRATION_SCORE: "What share of the population can discover/buy the product online?",
    IndicatorCode.MOBILE_CONNECTIVITY_SCORE: (
        "How widely are mobile lines held? Mobile subscriptions per 100 people, saturating at one "
        "line per person: above that the number counts SIM cards (prepaid, dual-SIM, M2M), not "
        "reach, so every market at or past one-per-person reads as fully served."
    ),
    IndicatorCode.BROADBAND_ADOPTION_SCORE: (
        "How much fixed broadband infrastructure is installed? Subscriptions per 100 people — one "
        "line serves a household, so this reads access quality rather than device count."
    ),
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
    IndicatorCode.CATEGORY_VOLUME_SCORE: (
        "How much online search demand exists for the product's whole CATEGORY here — context only, "
        "not scored: it is the scale against which this brand's own demand should be read. A brand "
        "with little demand in a market where the category is searched heavily is losing to "
        "competitors, not facing an absent market."
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

# The MOS itself (Cap. 5) — the single 0-100 market-opportunity score a country
# gets, the weighted TOPSIS aggregation of all dimensions. Its own meaning row so
# the final LLM can narrate the headline number, not only the parts.
MOS_MEANING: str = (
    "MARKET OPPORTUNITY SCORE — the single 0-100 verdict on how attractive this country is for "
    "selling this product right now, aggregating every dimension (demand volume and quality, macro "
    "demand structure, economic and digital readiness, market size, demographic sustainability) with "
    "Rank-Reciprocal priority weights via TOPSIS. Higher = a better place to sell. It is a RANKING "
    "signal ('where to sell first'), not a sales forecast: read it together with the dimension and "
    "signal breakdown to explain WHY a country lands where it does."
)

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


def display_score(score: float) -> float:
    """O número como ele é MOSTRADO: uma casa decimal. A faixa sai dele.

    Até 26/10/2026 a faixa saía do número cru e o número era arredondado
    depois: um MOS de 79,96 aparecia como 80,0 com a faixa "High", e não "Good"
    — a tela mostrava um número e uma faixa que não batiam com o corte da
    própria tabela. Quem lê confere a faixa pelo número que vê.
    """
    return round(float(score), 1)


def score_band(score: float) -> str:
    """0-100 normalized score -> Very Low/Low/Moderate/High/Very High, read
    off the displayed value."""
    shown = display_score(score)
    for upper_bound, label in _SCORE_BANDS:
        if shown < upper_bound:
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
    """0-100 MOS -> Cap. 5 §5.8 interpretation label, read off the displayed
    value (see display_score). The only MOS band in the system."""
    shown = display_score(mos)
    for upper_bound, label in _MOS_BANDS:
        if shown < upper_bound:
            return label
    return "Excellent Opportunity"


def band_key(band: str) -> str:
    """A chave estável de uma faixa ('Good Opportunity' -> 'good_opportunity').
    É o que o frontend compara; o rótulo é só o que ele mostra."""
    return band.strip().lower().replace(" ", "_")


# Os rótulos das faixas em cada idioma da análise, pela chave. São do método,
# não conteúdo editável: o corte é a tabela do Cap. 5 §5.8, e o nome de cada
# faixa vai junto com ele.
BAND_LABELS: dict[str, dict[str, str]] = {
    "en": {
        "very_low": "Very Low",
        "low": "Low",
        "moderate": "Moderate",
        "high": "High",
        "very_high": "Very High",
        "very_low_opportunity": "Very Low Opportunity",
        "low_opportunity": "Low Opportunity",
        "moderate_opportunity": "Moderate Opportunity",
        "good_opportunity": "Good Opportunity",
        "high_opportunity": "High Opportunity",
        "excellent_opportunity": "Excellent Opportunity",
    },
    "pt": {
        "very_low": "Muito baixo",
        "low": "Baixo",
        "moderate": "Moderado",
        "high": "Alto",
        "very_high": "Muito alto",
        "very_low_opportunity": "Oportunidade muito baixa",
        "low_opportunity": "Oportunidade baixa",
        "moderate_opportunity": "Oportunidade moderada",
        "good_opportunity": "Boa oportunidade",
        "high_opportunity": "Alta oportunidade",
        "excellent_opportunity": "Oportunidade excelente",
    },
    "es": {
        "very_low": "Muy bajo",
        "low": "Bajo",
        "moderate": "Moderado",
        "high": "Alto",
        "very_high": "Muy alto",
        "very_low_opportunity": "Oportunidad muy baja",
        "low_opportunity": "Oportunidad baja",
        "moderate_opportunity": "Oportunidad moderada",
        "good_opportunity": "Buena oportunidad",
        "high_opportunity": "Alta oportunidad",
        "excellent_opportunity": "Oportunidad excelente",
    },
}


def band_label(band: str, language: str) -> str:
    """O rótulo de uma faixa no idioma da análise (já resolvido por
    app.utils.language.analysis_language)."""
    return BAND_LABELS.get(language, BAND_LABELS["en"]).get(band_key(band), band)


def mos_presentation(mos: float | None, language: str) -> dict[str, float | str | None]:
    """O que se mostra de um MOS: o número com uma casa, a chave da faixa e o
    rótulo dela no idioma da análise — os três do MESMO valor arredondado.
    Usado em toda linha de resultado e em todo país do pacote de evidências."""
    if mos is None:
        return {"mos_display": None, "mos_band_key": None, "mos_band_label": None}
    band = mos_band(float(mos))
    return {
        "mos_display": display_score(mos),
        "mos_band_key": band_key(band),
        "mos_band_label": band_label(band, language),
    }
