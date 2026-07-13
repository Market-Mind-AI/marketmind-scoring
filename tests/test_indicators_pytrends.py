from unittest.mock import patch

import pytest

from app.domain.enums import AvailabilityStatus, IndicatorDirection
from app.services.indicators import pytrends as pytrends_indicators
from tests._external_db_mock import make_mock_engine, result_with_all, result_with_first


async def test_trend_score_reads_worldwide_country_value() -> None:
    # trend_score is the WORLDWIDE interest-by-country value for the country
    # (global 0-100 axis) — the MAX of the country's language variations
    # (query returns (MAX, COUNT)), not the per-country self-scaled series.
    engine = make_mock_engine([result_with_first((87.0, 2))])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        result = await pytrends_indicators.compute_trend_score("jamon iberico", "ES", "2026-07")

    assert result.availability_status == AvailabilityStatus.AVAILABLE
    assert result.raw_value == 87.0
    assert result.direction == IndicatorDirection.DIRECT


async def test_trend_score_clamps_to_100_safety_guard() -> None:
    # Anchor-chaining (>5 terms) can scale a value slightly past the axis —
    # a min(100) safety guard keeps the score on the 0-100 scale.
    engine = make_mock_engine([result_with_first((140.0, 3))])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        result = await pytrends_indicators.compute_trend_score("jamon iberico", "PT", "2026-07", term_language="pt")

    assert result.availability_status == AvailabilityStatus.AVAILABLE
    assert result.raw_value == 100.0


async def test_trend_score_unavailable_when_country_absent_from_worldwide_breakdown() -> None:
    # No rows for the country/language -> COUNT is 0 -> UNAVAILABLE.
    engine = make_mock_engine([result_with_first((0, 0))])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        result = await pytrends_indicators.compute_trend_score("jamon iberico", "ES", "2026-07")

    assert result.availability_status == AvailabilityStatus.UNAVAILABLE
    assert result.raw_value is None


async def test_trend_score_takes_max_localized_variation_by_language() -> None:
    # Data arbitration (2026-07-11): each country takes the MAX (winning local
    # name) among the real variations in ITS OWN language — count-independent,
    # so a language with more candidates isn't mechanically inflated.
    engine = make_mock_engine([result_with_first((71.0, 3))])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        result = await pytrends_indicators.compute_trend_score("jamon iberico", "PT", "2026-07", term_language="pt")

    assert result.availability_status == AvailabilityStatus.AVAILABLE
    assert result.raw_value == 71.0
    assert "term_language='pt'" in result.notes
    assert "MAX of 3 local variation(s)" in result.notes


async def test_growth_score_computes_expected_raw() -> None:
    series = [(10.0,), (10.0,), (10.0,), (20.0,), (20.0,), (20.0,), (40.0,), (40.0,), (40.0,)]
    engine = make_mock_engine([result_with_all(series)])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        result = await pytrends_indicators.compute_growth_score("air fryer", "ES", "2026-07")

    assert result.availability_status == AvailabilityStatus.AVAILABLE
    # k = min(30, 9 // 3) = 3; initial_avg=10, final_avg=40 -> ((40-10)/10)*100
    assert result.raw_value == pytest.approx(300.0)


async def test_growth_score_unavailable_with_fewer_than_six_observations() -> None:
    engine = make_mock_engine([result_with_all([(10.0,), (20.0,), (30.0,)])])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        result = await pytrends_indicators.compute_growth_score("air fryer", "ES", "2026-07")

    assert result.availability_status == AvailabilityStatus.UNAVAILABLE


async def test_growth_score_flat_zero_on_near_zero_baseline() -> None:
    # Low-base noise guard (2026-07-10): an almost-flat baseline near 0 makes
    # the growth ratio explode (a live run produced +1835%). Must return a
    # flat 0% (AVAILABLE) — not a spurious extreme, and not UNAVAILABLE
    # (which would drop the signal entirely).
    series = [(0.0,), (0.0,), (1.0,), (0.0,), (0.0,), (2.0,), (40.0,), (50.0,), (60.0,)]
    engine = make_mock_engine([result_with_all(series)])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        result = await pytrends_indicators.compute_growth_score("air fryer", "ES", "2026-07")

    assert result.availability_status == AvailabilityStatus.AVAILABLE
    assert result.raw_value == 0.0


async def test_demand_stability_score_computes_cv() -> None:
    engine = make_mock_engine([result_with_all([(10.0,), (20.0,), (30.0,)])])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        result = await pytrends_indicators.compute_demand_stability_score("air fryer", "ES", "2026-07")

    assert result.availability_status == AvailabilityStatus.AVAILABLE
    assert result.direction == IndicatorDirection.INVERSE
    # mean=20, population stddev=sqrt(200/3)=8.1650, CV=8.1650/20
    assert result.raw_value == pytest.approx(0.408248, rel=1e-4)


async def test_demand_stability_unavailable_when_mean_is_zero() -> None:
    engine = make_mock_engine([result_with_all([(0.0,), (0.0,)])])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        result = await pytrends_indicators.compute_demand_stability_score("air fryer", "ES", "2026-07")

    assert result.availability_status == AvailabilityStatus.UNAVAILABLE


async def test_regional_demand_score_computes_mean() -> None:
    # Per-country signal (geo=XX): mean of the country's own sub-region values.
    engine = make_mock_engine([result_with_all([(100.0,), (95.0,), (76.0,)])])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        result = await pytrends_indicators.compute_regional_demand_score("jamon iberico", "ES", "2026-07")

    assert result.availability_status == AvailabilityStatus.AVAILABLE
    assert result.raw_value == pytest.approx((100.0 + 95.0 + 76.0) / 3)


async def test_regional_demand_score_unavailable_when_no_regions() -> None:
    engine = make_mock_engine([result_with_all([])])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        result = await pytrends_indicators.compute_regional_demand_score("jamon iberico", "ES", "2026-07")

    assert result.availability_status == AvailabilityStatus.UNAVAILABLE


async def test_search_intent_score_weighted_classification() -> None:
    rows = [("amazon air fryer", 100.0), ("recetas air fryer", 20.0)]
    engine = make_mock_engine([result_with_all(rows)])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        result = await pytrends_indicators.compute_search_intent_score("air fryer", "2026-07")

    assert result.availability_status == AvailabilityStatus.AVAILABLE
    # amazon -> 1.0 * 100 = 100; recetas -> 0.4 * 20 = 8; total=108/120*100
    assert result.raw_value == pytest.approx(90.0)


async def test_search_intent_score_unavailable_when_no_queries() -> None:
    engine = make_mock_engine([result_with_all([])])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        result = await pytrends_indicators.compute_search_intent_score("air fryer", "2026-07")

    assert result.availability_status == AvailabilityStatus.UNAVAILABLE
