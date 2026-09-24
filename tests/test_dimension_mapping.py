import uuid
from datetime import datetime
from decimal import Decimal

from app.db.models.indicator_value import IndicatorValue
from app.domain.enums import IndicatorCode, IndicatorDimension
from app.services.mos.dimension_mapping import (
    DIMENSION_BY_INDICATOR,
    MANDATORY_INDICATORS,
    check_availability,
    compute_dimension_subscores,
)

# Every mandatory indicator except inflation, at a fixed 50.0 normalized score.
_ALL_MANDATORY_CODES = [c.value for c in MANDATORY_INDICATORS]


def _iv(code: str, score: float | None, *, availability_status: str = "available") -> IndicatorValue:
    return IndicatorValue(
        id=uuid.uuid4(),
        entity_key=None,
        country="USA",
        indicator_code=code,
        period_type="month",
        period_key="2026-07",
        source="worldbank",
        raw_value=Decimal("1"),
        normalized_score=None if score is None else Decimal(str(score)),
        direction="direct",
        calculation_window="test",
        availability_status=availability_status,
        notes=None,
        computed_at=datetime(2026, 7, 2),
    )


def _full_values() -> dict[str, IndicatorValue]:
    # worldbank_inflation_score is now a plain mandatory indicator (OECD path
    # removed 2026-07-08), so it's already in _ALL_MANDATORY_CODES.
    return {code: _iv(code, 50.0) for code in _ALL_MANDATORY_CODES}


class TestCheckAvailability:
    def test_full_set_is_eligible(self) -> None:
        assert check_availability(_full_values()) is None

    def test_missing_mandatory_indicator_is_excluded(self) -> None:
        values = _full_values()
        del values["gdp_per_capita_score"]
        reason = check_availability(values)
        assert reason is not None
        assert "gdp_per_capita_score" in reason

    def test_missing_inflation_is_excluded(self) -> None:
        values = _full_values()
        del values["worldbank_inflation_score"]
        reason = check_availability(values)
        assert reason is not None
        assert "worldbank_inflation_score" in reason

    def test_median_age_fit_absence_does_not_exclude(self) -> None:
        values = _full_values()
        assert "median_age_fit_score" not in values
        assert check_availability(values) is None

    def test_real_estate_growth_absence_does_not_exclude(self) -> None:
        values = _full_values()
        assert "real_estate_growth_score" not in values
        assert check_availability(values) is None

    def test_macro_demand_structure_indicators_are_mandatory(self) -> None:
        for code in (
            IndicatorCode.CONSUMER_SPENDING_SCORE,
            IndicatorCode.INVESTMENT_SCORE,
            IndicatorCode.NET_EXPORTS_SCORE,
            IndicatorCode.TRADE_OPENNESS_SCORE,
        ):
            assert code in MANDATORY_INDICATORS
            values = _full_values()
            del values[code.value]
            reason = check_availability(values)
            assert reason is not None
            assert code.value in reason

    def test_unavailable_status_counts_as_missing_even_if_row_present(self) -> None:
        values = _full_values()
        values["gdp_per_capita_score"] = _iv("gdp_per_capita_score", None, availability_status="unavailable")
        reason = check_availability(values)
        assert reason is not None
        assert "gdp_per_capita_score" in reason


class TestComputeDimensionSubscores:
    def test_averages_scores_within_a_dimension(self) -> None:
        # Demand is split (2026-07-16): VOLUME = trend_score alone (absolute
        # market size), QUALITY = mean of growth/stability/intent (size-
        # independent shape signals). Other dimensions still average their
        # indicators.
        values = {
            "trend_score": _iv("trend_score", 80.0),
            "growth_score": _iv("growth_score", 60.0),
            "demand_stability_score": _iv("demand_stability_score", 90.0),
            "search_intent_score": _iv("search_intent_score", 30.0),
        }
        subscores = compute_dimension_subscores(values)
        assert subscores[IndicatorDimension.DEMAND_VOLUME] == 80.0
        assert subscores[IndicatorDimension.DEMAND_QUALITY] == 60.0  # (60+90+30)/3

    def test_median_age_fit_contributes_only_when_present(self) -> None:
        without_maf = compute_dimension_subscores({"population_growth_score": _iv("population_growth_score", 50.0)})
        with_maf = compute_dimension_subscores(
            {
                "population_growth_score": _iv("population_growth_score", 50.0),
                "median_age_fit_score": _iv("median_age_fit_score", 100.0),
            }
        )
        assert without_maf[IndicatorDimension.DEMOGRAPHIC_SUSTAINABILITY] == 50.0
        assert with_maf[IndicatorDimension.DEMOGRAPHIC_SUSTAINABILITY] == 75.0

    def test_dimension_absent_entirely_when_no_indicator_available(self) -> None:
        subscores = compute_dimension_subscores({})
        assert subscores == {}

    def test_indicator_with_none_score_is_skipped(self) -> None:
        values = {"trend_score": _iv("trend_score", None, availability_status="unavailable")}
        assert compute_dimension_subscores(values) == {}

    def test_macro_demand_structure_averages_its_four_indicators(self) -> None:
        values = {
            "consumer_spending_score": _iv("consumer_spending_score", 80.0),
            "investment_score": _iv("investment_score", 60.0),
            "net_exports_score": _iv("net_exports_score", 40.0),
            "trade_openness_score": _iv("trade_openness_score", 20.0),
        }
        subscores = compute_dimension_subscores(values)
        assert subscores[IndicatorDimension.MACRO_DEMAND_STRUCTURE] == 50.0

    def test_real_estate_growth_contributes_to_economic_readiness_only_when_present(self) -> None:
        without = compute_dimension_subscores({"gdp_per_capita_score": _iv("gdp_per_capita_score", 50.0)})
        with_growth = compute_dimension_subscores(
            {
                "gdp_per_capita_score": _iv("gdp_per_capita_score", 50.0),
                "real_estate_growth_score": _iv("real_estate_growth_score", 100.0),
            }
        )
        assert without[IndicatorDimension.ECONOMIC_READINESS] == 50.0
        assert with_growth[IndicatorDimension.ECONOMIC_READINESS] == 75.0


class TestRegionalDemandIsContextOnly:
    def test_regional_demand_never_maps_to_a_dimension(self) -> None:
        # Self-scaled per country (interest_by_region is relative to each
        # country's OWN top region) -> not cross-country comparable, so it's
        # kept as narrative context and never scored into Demand (2026-07-14).
        assert IndicatorCode.REGIONAL_DEMAND_SCORE not in DIMENSION_BY_INDICATOR

    def test_regional_demand_never_mandatory(self) -> None:
        assert IndicatorCode.REGIONAL_DEMAND_SCORE not in MANDATORY_INDICATORS

    def test_regional_demand_ignored_even_if_a_score_is_present(self) -> None:
        # Even if a normalized_score is attached (it is — regional is still
        # computed for context), it must not leak into the Demand sub-score.
        values = {
            "trend_score": _iv("trend_score", 80.0),
            "regional_demand_score": _iv("regional_demand_score", 0.0),
        }
        subscores = compute_dimension_subscores(values)
        assert subscores[IndicatorDimension.DEMAND_VOLUME] == 80.0
        assert IndicatorDimension.MARKET_SIZE not in subscores  # regional leaked nowhere


class TestGovernmentExpenditureIsContextOnly:
    def test_government_expenditure_never_maps_to_a_dimension(self) -> None:
        assert IndicatorCode.GOVERNMENT_EXPENDITURE_SCORE not in DIMENSION_BY_INDICATOR

    def test_government_expenditure_never_mandatory(self) -> None:
        assert IndicatorCode.GOVERNMENT_EXPENDITURE_SCORE not in MANDATORY_INDICATORS

    def test_government_expenditure_ignored_even_if_a_score_is_present(self) -> None:
        # Defensive: even if a normalized_score ever got attached to this
        # code (it shouldn't, per NormalizationMethod.CONTEXT_ONLY), it must
        # not leak into any dimension sub-score.
        values = {
            "gdp_per_capita_score": _iv("gdp_per_capita_score", 50.0),
            "government_expenditure_score": _iv("government_expenditure_score", 0.0),
        }
        subscores = compute_dimension_subscores(values)
        assert subscores[IndicatorDimension.ECONOMIC_READINESS] == 50.0


class TestDigitalReadinessWeighting:
    """Cap. 4 / migration 044 — Digital Readiness deixou de ser média simples.

    O caso real que motivou: análise de café 2026-09-23, Brasil (Internet 84.5,
    Chip 101.93 -> satura em 100, Banda larga 24.08 -> 43.8) contra Colômbia
    (79.3 / 174.09 -> 100 / 17.22 -> 31.3). Antes a dimensão dava BRA 55.5 e
    COL 83.0 e invertia o ranking inteiro; com a régua saturada e os pesos, dá
    BRA acima de COL.
    """

    def _digital(self, internet: float, mobile: float, broadband: float | None) -> float:
        values = {
            IndicatorCode.INTERNET_PENETRATION_SCORE.value: _iv(
                IndicatorCode.INTERNET_PENETRATION_SCORE.value, internet
            ),
            IndicatorCode.MOBILE_CONNECTIVITY_SCORE.value: _iv(
                IndicatorCode.MOBILE_CONNECTIVITY_SCORE.value, mobile
            ),
        }
        if broadband is not None:
            values[IndicatorCode.BROADBAND_ADOPTION_SCORE.value] = _iv(
                IndicatorCode.BROADBAND_ADOPTION_SCORE.value, broadband
            )
        return compute_dimension_subscores(values)[IndicatorDimension.DIGITAL_READINESS]

    def test_weights_are_internet_then_broadband_then_mobile(self) -> None:
        # 2.0 / 1.5 / 1.0 sobre 4.5. Só o de maior peso em 100 puxa mais do que
        # só o de menor peso em 100.
        so_internet = self._digital(100.0, 0.0, 0.0)
        so_mobile = self._digital(0.0, 100.0, 0.0)
        so_broadband = self._digital(0.0, 0.0, 100.0)

        assert so_internet > so_broadband > so_mobile
        assert round(so_internet, 2) == round(100 * 2.0 / 4.5, 2)
        assert round(so_broadband, 2) == round(100 * 1.5 / 4.5, 2)
        assert round(so_mobile, 2) == round(100 * 1.0 / 4.5, 2)

    def test_brasil_supera_colombia_com_reguas_novas(self) -> None:
        brasil = self._digital(84.5, 100.0, 43.8)
        colombia = self._digital(79.3, 100.0, 31.3)

        assert brasil > colombia
        # A inversão antiga era de 27.5 pontos CONTRA o Brasil; conferimos que
        # virou e com folga, não por arredondamento.
        assert brasil - colombia > 5.0

    def test_pais_sem_broadband_renormaliza_em_vez_de_ser_punido(self) -> None:
        """Durante o backfill um país pode não ter o indicador. O peso dele tem
        de ser repartido entre os outros dois, nunca contar como zero."""
        sem = self._digital(80.0, 60.0, None)
        com_zero = self._digital(80.0, 60.0, 0.0)

        assert round(sem, 4) == round((80.0 * 2.0 + 60.0 * 1.0) / 3.0, 4)
        assert sem > com_zero

    def test_broadband_nao_e_obrigatorio_durante_o_backfill(self) -> None:
        values = _full_values()
        assert IndicatorCode.BROADBAND_ADOPTION_SCORE.value not in values
        assert check_availability(values) is None

    def test_demais_dimensoes_seguem_media_simples(self) -> None:
        """A ponderação é só de Digital Readiness — todo indicador fora de
        WEIGHT_IN_DIMENSION pesa 1.0, então Market Size continua igual."""
        values = {
            IndicatorCode.POPULATION_SCALE_SCORE.value: _iv(IndicatorCode.POPULATION_SCALE_SCORE.value, 90.0),
            IndicatorCode.URBANIZATION_SCORE.value: _iv(IndicatorCode.URBANIZATION_SCORE.value, 70.0),
        }
        assert compute_dimension_subscores(values)[IndicatorDimension.MARKET_SIZE] == 80.0
