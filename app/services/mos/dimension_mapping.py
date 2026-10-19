"""Cap. 5 §5.5 correction — 2-stage aggregation: the ~16 atomic indicators
(Cap. 4) are grouped into dimensions before the weights apply, so Demand
(5 indicators) isn't diluted against the macro block (11 indicators) just
for having fewer atomic signals. See memory marketmind_mos_engine."""

from app.db.models.indicator_value import IndicatorValue
from app.domain.enums import AvailabilityStatus, IndicatorCode, IndicatorDimension

DIMENSION_BY_INDICATOR: dict[IndicatorCode, IndicatorDimension] = {
    # DEMAND split into VOLUME (absolute market size / sales potential) and
    # QUALITY (shape signals) — 2026-07-16. trend_score is the absolute
    # search-volume LEVEL (linear-normalized), the sole VOLUME signal; the
    # other three are size-independent quality ratios. Averaging them together
    # let a tiny high-quality market tie a huge one; separating makes volume a
    # dimension of its own so it isn't diluted. See marketmind_mos_engine.
    IndicatorCode.TREND_SCORE: IndicatorDimension.DEMAND_VOLUME,
    IndicatorCode.GROWTH_SCORE: IndicatorDimension.DEMAND_QUALITY,
    IndicatorCode.DEMAND_STABILITY_SCORE: IndicatorDimension.DEMAND_QUALITY,
    IndicatorCode.SEARCH_INTENT_SCORE: IndicatorDimension.DEMAND_QUALITY,
    # REGIONAL_DEMAND_SCORE is deliberately NOT scored into Demand (2026-07-14):
    # it's the per-country `interest_by_region` mean, self-scaled to each
    # country's OWN top region, so it is NOT cross-country comparable (a
    # geographically-spread market reads high, a concentrated one low, on
    # different rulers) — mixing it into a cross-country MOS distorted the
    # ranking (e.g. it inflated Mexico over the USA for air fryer). The other
    # four Demand signals ARE comparable: trend_score/search_intent are
    # worldwide/composition, growth/stability are scale-invariant ratios.
    # regional_demand is still computed and stored as narrative CONTEXT for the
    # LLM (like government_expenditure), just never aggregated. See
    # marketmind_mos_engine.
    IndicatorCode.INTERNET_PENETRATION_SCORE: IndicatorDimension.DIGITAL_READINESS,
    IndicatorCode.MOBILE_CONNECTIVITY_SCORE: IndicatorDimension.DIGITAL_READINESS,
    IndicatorCode.BROADBAND_ADOPTION_SCORE: IndicatorDimension.DIGITAL_READINESS,
    IndicatorCode.GDP_PER_CAPITA_SCORE: IndicatorDimension.ECONOMIC_READINESS,
    IndicatorCode.GDP_GROWTH_SCORE: IndicatorDimension.ECONOMIC_READINESS,
    IndicatorCode.WORLDBANK_INFLATION_SCORE: IndicatorDimension.ECONOMIC_READINESS,
    IndicatorCode.REAL_ESTATE_GROWTH_SCORE: IndicatorDimension.ECONOMIC_READINESS,
    IndicatorCode.POPULATION_SCALE_SCORE: IndicatorDimension.MARKET_SIZE,
    IndicatorCode.URBANIZATION_SCORE: IndicatorDimension.MARKET_SIZE,
    IndicatorCode.POPULATION_GROWTH_SCORE: IndicatorDimension.DEMOGRAPHIC_SUSTAINABILITY,
    IndicatorCode.NET_MIGRATION_RATE_SCORE: IndicatorDimension.DEMOGRAPHIC_SUSTAINABILITY,
    IndicatorCode.MEDIAN_AGE_FIT_SCORE: IndicatorDimension.DEMOGRAPHIC_SUSTAINABILITY,
    # Macro Demand Structure (2026-07-08, GDP = C+I+G+NX) — Government
    # Expenditure (G) is deliberately NOT here: it's context-only (no
    # defensible mayor/menor direction), never enters a dimension. See
    # marketmind_mos_engine.
    IndicatorCode.CONSUMER_SPENDING_SCORE: IndicatorDimension.MACRO_DEMAND_STRUCTURE,
    IndicatorCode.INVESTMENT_SCORE: IndicatorDimension.MACRO_DEMAND_STRUCTURE,
    IndicatorCode.NET_EXPORTS_SCORE: IndicatorDimension.MACRO_DEMAND_STRUCTURE,
    IndicatorCode.TRADE_OPENNESS_SCORE: IndicatorDimension.MACRO_DEMAND_STRUCTURE,
}

# Peso RELATIVO de cada indicador DENTRO da sua dimensão. Ausente daqui = 1.0,
# que é a média simples que valia para todas as dimensões até 2026-09-24 e
# continua valendo para todas menos Digital Readiness.
#
# Por que deixou de ser média simples. Digital Readiness tratava
# internet_penetration e mobile_connectivity como iguais, 50% cada, e os dois
# não são igualmente confiáveis. O primeiro é IT.NET.USER.ZS, o share da
# população que usa internet -- mede pessoas alcançáveis, que é exatamente o que
# a dimensão quer saber. O segundo é IT.CEL.SETS.P2, assinaturas de celular por
# 100 habitantes -- mede CHIPS, não pessoas, e acima de ~100 por 100 habitantes
# ele deixa de medir acesso e passa a medir comportamento de chip (pré-pago,
# dual-SIM, linhas M2M).
#
# O caso que expôs isso, café, 2026-09-23: o Brasil (101,93 chips/100 hab., uma
# linha por pessoa, mercado consolidado) pontuou 26,6 e recebeu o texto
# "conectividade móvel fraca na maior parte da população". A Colômbia (174/100,
# muito pré-pago e dual-SIM) pontuou 86,7 e recebeu "excelente, mercado
# mobile-first". Com 50% do peso da dimensão, essa inversão sozinha virou o
# ranking inteiro: o Brasil vencia em 4 das 7 dimensões, incluindo as duas de
# maior peso, e perdeu por 0,80 ponto.
#
# broadband_adoption entra entre os dois: IT.NET.BBND.P2 mede infraestrutura
# domiciliar real e não tem o teto degenerado do chip, mas cobre só a parte fixa
# do acesso, então pesa menos que a penetração de internet.
#
# Os pesos são relativos e renormalizados pelos indicadores DISPONÍVEIS (ver
# compute_dimension_subscores), então um país sem broadband reparte o peso entre
# os outros dois em vez de ser penalizado por uma ausência.
WEIGHT_IN_DIMENSION: dict[IndicatorCode, float] = {
    IndicatorCode.INTERNET_PENETRATION_SCORE: 2.0,
    IndicatorCode.BROADBAND_ADOPTION_SCORE: 1.5,
    IndicatorCode.MOBILE_CONNECTIVITY_SCORE: 1.0,
}

_DEFAULT_WEIGHT_IN_DIMENSION = 1.0


# Mandatory = present or the country is excluded from the MOS (Cap. 5 §5.3).
# Only two indicators are non-mandatory, and both for structural reasons (not
# collection fragility):
#   - Median Age Fit: only defined when the Product Profile has a target age
#     range (Cap. 4 §4.5.3) — conditional by design.
#   - Real Estate Growth: OECD-only coverage (Cap. 4 §4.4.3) — non-OECD
#     countries structurally cannot have it.
# Everything else — including ALL five Demand signals (trend_score plus the
# four per-country PyTrends signals) — is mandatory (user principle 2026-07-11:
# the MOS must never be computed on partial data; every country carries the
# SAME indicator set on the same 0-100 ruler, so the scores stay comparable).
# Collection must therefore complete for every country before any score/MOS is
# computed (see analysis_service completeness gate); PyTrends rate limits are
# handled by reliable retry + the DataForSEO live fallback, never by skipping.
MANDATORY_INDICATORS: frozenset[IndicatorCode] = frozenset(DIMENSION_BY_INDICATOR) - {
    IndicatorCode.MEDIAN_AGE_FIT_SCORE,
    IndicatorCode.REAL_ESTATE_GROWTH_SCORE,
    # Broadband Adoption entra NÃO obrigatório (2026-09-24) por um motivo de
    # transição, não estrutural: IT.NET.BBND.P2 estava declarado no catálogo do
    # marketmind-worldbank desde sempre mas nunca tinha sido coletado -- zero
    # observações para os 31 países. Torná-lo obrigatório de saída excluiria
    # TODA análise até o backfill terminar.
    #
    # Deve virar obrigatório assim que a cobertura fechar, para restaurar o
    # princípio de 2026-07-11 (todo país carrega o MESMO conjunto de
    # indicadores). Enquanto isso o peso relativo é renormalizado pelos
    # disponíveis, então um país sem broadband não fica com a dimensão furada --
    # fica com a dimensão medida pelos dois indicadores que ele tem.
    IndicatorCode.BROADBAND_ADOPTION_SCORE,
}


def _is_present(indicator_values: dict[str, IndicatorValue], code: IndicatorCode) -> bool:
    iv = indicator_values.get(code.value)
    if iv is None or iv.normalized_score is None:
        return False
    return iv.availability_status != AvailabilityStatus.UNAVAILABLE


def check_availability(indicator_values: dict[str, IndicatorValue]) -> str | None:
    """Cap. 5 §5.3: every mandatory indicator must be present. Returns None
    if the country is eligible for MOS, or a human-readable exclusion
    reason otherwise (never silently drops the country)."""
    missing = [code.value for code in MANDATORY_INDICATORS if not _is_present(indicator_values, code)]
    if missing:
        return "Missing mandatory indicator(s): " + ", ".join(missing)
    return None


def compute_dimension_subscores(indicator_values: dict[str, IndicatorValue]) -> dict[IndicatorDimension, float]:
    """Sub-score per dimension = mean of the normalized scores of every
    available indicator in that dimension, all on the same 0-100 ruler
    (Median Age Fit only contributes when present — it's conditional, not
    mandatory). Demand averages its FOUR scored signals like any other
    dimension: every one is mandatory and collected for every country before
    this runs (completeness gate), so the set is uniform and the plain mean
    stays comparable across countries. All four are cross-country comparable —
    trend_score comes from the WORLDWIDE breakdown, search_intent is a
    composition, and growth/stability are scale-invariant ratios.
    regional_demand is intentionally excluded (self-scaled per country, not
    comparable — see DIMENSION_BY_INDICATOR); it stays as narrative context.

    Desde 2026-09-24 a média é PONDERADA por WEIGHT_IN_DIMENSION. Todo indicador
    não listado lá pesa 1.0, então toda dimensão exceto Digital Readiness
    continua sendo exatamente a média simples de antes. O peso é renormalizado
    pelos indicadores efetivamente presentes, e não pelo conjunto teórico da
    dimensão: um país sem Broadband Adoption reparte aquele peso entre os
    outros dois em vez de ficar com a dimensão diluída por uma ausência."""
    by_dimension: dict[IndicatorDimension, list[tuple[float, float]]] = {}
    for code, dimension in DIMENSION_BY_INDICATOR.items():
        iv = indicator_values.get(code.value)
        if iv is None or iv.normalized_score is None:
            continue
        weight = WEIGHT_IN_DIMENSION.get(code, _DEFAULT_WEIGHT_IN_DIMENSION)
        by_dimension.setdefault(dimension, []).append((float(iv.normalized_score), weight))

    subscores: dict[IndicatorDimension, float] = {}
    for dimension, pairs in by_dimension.items():
        total_weight = sum(weight for _score, weight in pairs)
        if total_weight <= 0:
            continue
        subscores[dimension] = sum(score * weight for score, weight in pairs) / total_weight
    return subscores
