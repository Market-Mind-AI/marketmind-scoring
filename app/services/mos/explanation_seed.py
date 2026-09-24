"""Seed defaults for the metric_explanations + metric_band_phrases tables
(migration 016). The DB rows are the EDITABLE runtime source; this module holds
only the initial content. Meanings are imported from interpretation.py (their
single canonical, completeness-tested home); the human labels, the direction
flag, and the per-band phrases live here. The band phrases feed the final LLM
explanation, which narrates each score's number in words (see
explanation_client.py) — so each is a short, concrete, business-toned reading of
what that band means for that specific metric."""

from app.services.mos.interpretation import DIMENSION_MEANING, INDICATOR_MEANING, MOS_MEANING

INDICATOR_BANDS: list[str] = ["Very Low", "Low", "Moderate", "High", "Very High"]
MOS_BANDS: list[str] = [
    "Very Low Opportunity",
    "Low Opportunity",
    "Moderate Opportunity",
    "Good Opportunity",
    "High Opportunity",
    "Excellent Opportunity",
]

# code -> (metric_kind, label, higher_is_better)
# higher_is_better=None marks a direction-neutral, context-only metric (no
# good/bad valence — describe the level factually, not as favorable/unfavorable).
METRIC_META: dict[str, tuple[str, str, bool | None]] = {
    # --- indicators ---
    "trend_score": ("indicator", "Demand Volume", True),
    "growth_score": ("indicator", "Demand Growth", True),
    "demand_stability_score": ("indicator", "Demand Stability", True),
    "regional_demand_score": ("indicator", "Regional Demand Spread", None),
    "search_intent_score": ("indicator", "Purchase Intent", True),
    "gdp_per_capita_score": ("indicator", "GDP per Capita", True),
    "internet_penetration_score": ("indicator", "Internet Penetration", True),
    "mobile_connectivity_score": ("indicator", "Mobile Connectivity", True),
    "broadband_adoption_score": ("indicator", "Broadband Adoption", True),
    "gdp_growth_score": ("indicator", "GDP Growth", True),
    "population_scale_score": ("indicator", "Population Scale", True),
    "urbanization_score": ("indicator", "Urbanization", True),
    "worldbank_inflation_score": ("indicator", "Inflation", True),
    "population_growth_score": ("indicator", "Population Growth", True),
    "net_migration_rate_score": ("indicator", "Net Migration", True),
    "median_age_fit_score": ("indicator", "Median Age Fit", True),
    "consumer_spending_score": ("indicator", "Consumer Spending", True),
    "government_expenditure_score": ("indicator", "Government Expenditure", None),
    "category_volume_score": ("indicator", "Category Demand", None),
    "investment_score": ("indicator", "Investment", True),
    "net_exports_score": ("indicator", "Net Exports", True),
    "trade_openness_score": ("indicator", "Trade Openness", True),
    "real_estate_growth_score": ("indicator", "Real Estate Growth", True),
    # --- dimensions ---
    "demand_volume": ("dimension", "Demand Volume", True),
    "demand_quality": ("dimension", "Demand Quality", True),
    "macro_demand_structure": ("dimension", "Macro Demand Structure", True),
    "market_size": ("dimension", "Market Size", True),
    "economic_readiness": ("dimension", "Economic Readiness", True),
    "digital_readiness": ("dimension", "Digital Readiness", True),
    "demographic_sustainability": ("dimension", "Demographic Sustainability", True),
    # --- mos ---
    "mos": ("mos", "Market Opportunity Score", True),
}

# code -> {band_label: phrase}. Indicators & dimensions use INDICATOR_BANDS
# (5 bands); "mos" uses MOS_BANDS (6 bands). Every code in METRIC_META must
# appear here with a phrase for EVERY band in its band set.
BAND_PHRASES: dict[str, dict[str, str]] = {
    # --- indicators ---
    "trend_score": {
        "Very Low": "Almost nobody is searching to buy this product here",
        "Low": "Few active buyers - purchase demand is thin",
        "Moderate": "A modest but real base of people wanting to buy",
        "High": "Strong purchase demand - many active buyers in this market",
        "Very High": "Enormous purchase demand - a huge number of active buyers",
    },
    "growth_score": {
        "Very Low": "Demand is falling sharply - interest is drying up",
        "Low": "Demand is slipping - a gently declining market",
        "Moderate": "Demand is roughly flat, holding steady over time",
        "High": "Demand is climbing - a growing, expanding market",
        "Very High": "Demand is surging - interest is rising fast",
    },
    "demand_stability_score": {
        "Very Low": "Demand swings wildly - highly erratic and unpredictable",
        "Low": "Demand is volatile and hard to plan around",
        "Moderate": "Demand is fairly steady with occasional swings",
        "High": "Demand is stable and dependable month to month",
        "Very High": "Demand is very stable - consistent and easy to forecast",
    },
    "regional_demand_score": {
        "Very Low": "Interest is concentrated in just a few regions",
        "Low": "Interest clusters in a handful of leading regions",
        "Moderate": "Interest is moderately spread across the regions",
        "High": "Interest reaches most regions of the country",
        "Very High": "Interest is spread evenly across the whole country",
    },
    "search_intent_score": {
        "Very Low": "Searches show mostly idle curiosity, not intent to buy",
        "Low": "Weak buying signals - browsing outweighs buying here",
        "Moderate": "A fair mix of buyers and casual browsers",
        "High": "Searches signal clear intent to purchase the product",
        "Very High": "Searches show strong, ready-to-buy purchase intent",
    },
    "gdp_per_capita_score": {
        "Very Low": "Very limited average purchasing power in this market",
        "Low": "Low purchasing power constrains what people can spend",
        "Moderate": "Moderate purchasing power fits mainstream price points",
        "High": "Strong purchasing power - customers can afford premium goods",
        "Very High": "Very high purchasing power - an affluent, high-spending market",
    },
    "internet_penetration_score": {
        "Very Low": "Most people cannot reach the product online",
        "Low": "Limited online reach - many people remain offline",
        "Moderate": "About half the population can shop online",
        "High": "Most people can discover and buy online",
        "Very High": "Nearly everyone is online and reachable digitally",
    },
    # Reescritos em 2026-09-24 junto com a saturação da régua. Os textos antigos
    # falavam em qualidade de conectividade ("excelente", "fraca"), que é o que
    # o nome do indicador sugere e não o que ele mede: são assinaturas por 100
    # habitantes, ou seja, linhas. Com a régua saturando em uma linha por
    # pessoa, as faixas voltam a descrever o que existe -- quantas pessoas têm
    # linha própria --, e a de cima deixa de premiar quem tem muito chip por
    # habitante como se fosse infraestrutura melhor.
    "mobile_connectivity_score": {
        "Very Low": "Very few people hold a mobile line of their own",
        "Low": "Mobile lines reach only part of the population",
        "Moderate": "About half the population holds its own mobile line",
        "High": "Most people hold their own mobile line",
        "Very High": "At least one mobile line per person - mobile reach is saturated",
    },
    "broadband_adoption_score": {
        "Very Low": "Almost no fixed broadband infrastructure in place",
        "Low": "Fixed broadband reaches a small share of households",
        "Moderate": "Fixed broadband reaches a fair share of households",
        "High": "Fixed broadband is widely installed across households",
        "Very High": "Dense fixed broadband coverage - strong access infrastructure",
    },
    "gdp_growth_score": {
        "Very Low": "The economy is contracting sharply right now",
        "Low": "The economy is stagnant or slightly shrinking",
        "Moderate": "The economy is growing at a modest pace",
        "High": "The economy is expanding briskly",
        "Very High": "The economy is booming - rapid economic expansion",
    },
    "population_scale_score": {
        "Very Low": "A very small addressable population overall",
        "Low": "A small population limits the potential audience",
        "Moderate": "A mid-sized population to address",
        "High": "A large population offers broad reach",
        "Very High": "A vast population - an enormous addressable audience",
    },
    "urbanization_score": {
        "Very Low": "A largely rural population - hard logistics and reach",
        "Low": "Mostly rural - retail and delivery reach is limited",
        "Moderate": "A balanced urban and rural mix",
        "High": "Mostly urban - easy logistics and retail reach",
        "Very High": "Highly urbanized - very easy to reach and serve",
    },
    "worldbank_inflation_score": {
        "Very Low": "Runaway inflation is sharply eroding household purchasing power",
        "Low": "High inflation is squeezing what households can spend",
        "Moderate": "Moderate inflation with some pressure on prices",
        "High": "Inflation is low - prices are largely under control",
        "Very High": "Prices are very stable, purchasing power well preserved",
    },
    "population_growth_score": {
        "Very Low": "The population is shrinking fast - a contracting base",
        "Low": "The population is slowly declining",
        "Moderate": "The population is holding roughly steady",
        "High": "The population is growing steadily",
        "Very High": "The population is growing rapidly - an expanding base",
    },
    "net_migration_rate_score": {
        "Very Low": "The country is losing many people to emigration",
        "Low": "A net outflow of people through migration",
        "Moderate": "Migration into and out of the country is balanced",
        "High": "The country is attracting new residents on balance",
        "Very High": "A strong inflow of migrants is swelling the market",
    },
    "median_age_fit_score": {
        "Very Low": "The typical age is far from the product's target buyers",
        "Low": "The age profile fits the target audience poorly",
        "Moderate": "The age profile partly matches the target buyers",
        "High": "The age profile fits the target audience well",
        "Very High": "The typical age matches the target buyers almost perfectly",
    },
    "consumer_spending_score": {
        "Very Low": "Households drive very little of this economy",
        "Low": "Consumer spending plays a limited economic role",
        "Moderate": "Consumer spending is a moderate share of the economy",
        "High": "Household consumption powers much of the economy",
        "Very High": "A consumption-driven economy - households spend heavily",
    },
    "government_expenditure_score": {
        "Very Low": "The economy is driven mostly by private activity",
        "Low": "Government plays a small role in the economy",
        "Moderate": "Public and private spending are fairly balanced",
        "High": "Government is a large presence in the economy",
        "Very High": "The economy leans heavily on public spending",
    },
    "category_volume_score": {
        "Very Low": "The category itself is barely searched here",
        "Low": "The category draws little search demand here",
        "Moderate": "The category has moderate search demand here",
        "High": "The category is searched heavily here",
        "Very High": "The category is one of this market's big search subjects",
    },
    "investment_score": {
        "Very Low": "Very little is being invested in productive capacity",
        "Low": "Low investment in infrastructure and capital",
        "Moderate": "A moderate level of capital investment",
        "High": "Strong investment in infrastructure and productive capacity",
        "Very High": "Heavy investment fueling future productive capacity",
    },
    "net_exports_score": {
        "Very Low": "A heavy net importer with a large trade deficit",
        "Low": "Imports outweigh exports - a trade deficit",
        "Moderate": "Exports and imports are roughly in balance",
        "High": "A net exporter running a trade surplus",
        "Very High": "A strong net exporter with a large trade surplus",
    },
    "trade_openness_score": {
        "Very Low": "A largely closed economy - hard for foreign goods to enter",
        "Low": "Limited trade openness complicates market entry",
        "Moderate": "Moderately open to international trade",
        "High": "Open to trade - a smooth path for foreign products",
        "Very High": "A highly open economy - very easy for imports to enter",
    },
    "real_estate_growth_score": {
        "Very Low": "Property prices are falling sharply - a cooling market",
        "Low": "Property prices are softening across the market",
        "Moderate": "Property prices are roughly flat",
        "High": "Property prices are rising - a warming market",
        "Very High": "Property prices are surging - a booming market",
    },
    # --- dimensions ---
    "demand_volume": {
        "Very Low": "Barely any real purchase demand exists here",
        "Low": "A thin pool of actual buyers in this market",
        "Moderate": "A solid, mid-sized base of real buyers",
        "High": "A large base of people actively wanting to buy",
        "Very High": "A massive pool of real buyers - top-tier market size",
    },
    "demand_quality": {
        "Very Low": "Demand is weak, erratic, and rarely purchase-driven",
        "Low": "Demand quality is poor - shaky and browse-heavy",
        "Moderate": "Demand quality is decent but somewhat uneven",
        "High": "High-quality demand - growing, steady, and purchase-minded",
        "Very High": "Excellent demand - strongly growing, stable, and ready to buy",
    },
    "macro_demand_structure": {
        "Very Low": "The macro economy poorly supports consumer demand",
        "Low": "Weak macro fundamentals for consumer demand",
        "Moderate": "Moderately supportive macro demand conditions",
        "High": "Macro structure strongly supports consumer demand",
        "Very High": "A very favorable macro composition for consumer demand",
    },
    "market_size": {
        "Very Low": "A very small addressable market overall",
        "Low": "A limited addressable audience to work with",
        "Moderate": "A mid-sized addressable market to target",
        "High": "A large addressable audience to reach",
        "Very High": "A vast addressable market with wide reach",
    },
    "economic_readiness": {
        "Very Low": "Weak purchasing power and unstable prices",
        "Low": "Limited spending power and a shaky economic footing",
        "Moderate": "Moderate purchasing power and economic stability",
        "High": "Strong purchasing power and a stable economy",
        "Very High": "An affluent, stable economy fully ready to spend",
    },
    "digital_readiness": {
        "Very Low": "Very few can discover or buy the product online",
        "Low": "Limited digital reach for selling online",
        "Moderate": "Moderate digital access for online commerce",
        "High": "Strong digital reach - most can buy online",
        "Very High": "A fully digital market - nearly everyone can buy online",
    },
    "demographic_sustainability": {
        "Very Low": "Long-term demographics point to a shrinking market",
        "Low": "Demographic trends are unfavorable over the long run",
        "Moderate": "Demographics are broadly stable over the long term",
        "High": "Favorable long-term demographic trends support the market",
        "Very High": "Very strong long-term demographic momentum",
    },
    # --- mos ---
    "mos": {
        "Very Low Opportunity": "Avoid for now - a poor place to sell this product",
        "Low Opportunity": "A weak market - deprioritize against better options",
        "Moderate Opportunity": "A middling market - viable but not a priority",
        "Good Opportunity": "A solid market worth pursuing",
        "High Opportunity": "A strong market - prioritize for early entry",
        "Excellent Opportunity": "A top-priority market to enter first",
    },
}


def _meaning(code: str, kind: str) -> str:
    if kind == "mos":
        return MOS_MEANING
    for indicator, text in INDICATOR_MEANING.items():
        if indicator.value == code:
            return text
    for dimension, text in DIMENSION_MEANING.items():
        if dimension.value == code:
            return text
    return ""


def metric_rows() -> list[dict[str, object]]:
    return [
        {
            "metric_code": code,
            "metric_kind": kind,
            "label": label,
            "meaning": _meaning(code, kind),
            "higher_is_better": hib,
        }
        for code, (kind, label, hib) in METRIC_META.items()
    ]


def band_phrase_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for code, phrases in BAND_PHRASES.items():
        kind = METRIC_META[code][0]
        order = MOS_BANDS if kind == "mos" else INDICATOR_BANDS
        for band, phrase in phrases.items():
            rows.append({"metric_code": code, "band": band, "phrase": phrase, "ordinal": order.index(band)})
    return rows
