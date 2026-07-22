"""The per-score explanation sentence is built deterministically from the
catalog (label + band phrase + optional concrete detail). These pin the
templating so the phrase the LLM reads always names the score, its band, and
its number."""

from app.services.mos.explanation_builder import ExplanationCatalog, MetricEntry


def _catalog() -> ExplanationCatalog:
    return ExplanationCatalog(
        metrics={
            "trend_score": MetricEntry(
                kind="indicator",
                label="Demand Volume",
                meaning="how many people want it",
                higher_is_better=True,
                band_phrases={
                    "High": "Strong purchase demand - many active buyers in this market",
                    "Very Low": "Almost nobody is searching to buy this product here",
                },
            ),
            "mos": MetricEntry(
                kind="mos",
                label="Market Opportunity Score",
                meaning="overall verdict",
                higher_is_better=True,
                band_phrases={"Good Opportunity": "A solid market worth pursuing"},
            ),
        }
    )


def test_render_combines_label_band_score_and_phrase() -> None:
    out = _catalog().render(code="trend_score", score=76.3, band="High")
    assert out == "Demand Volume - High (76/100): Strong purchase demand - many active buyers in this market."


def test_render_appends_concrete_detail() -> None:
    out = _catalog().render(
        code="trend_score", score=76.3, band="High", detail="about 1,526,130 absolute monthly searches"
    )
    assert out is not None
    assert out.endswith("about 1,526,130 absolute monthly searches.")
    assert "Demand Volume - High (76/100)" in out


def test_render_mos_band() -> None:
    out = _catalog().render(code="mos", score=61.9, band="Good Opportunity")
    assert out == "Market Opportunity Score - Good Opportunity (62/100): A solid market worth pursuing."


def test_render_unknown_metric_is_none() -> None:
    assert _catalog().render(code="not_a_metric", score=50.0, band="Moderate") is None


def test_render_missing_score_is_flagged() -> None:
    out = _catalog().render(code="trend_score", score=None, band=None)
    assert out == "Demand Volume: score unavailable."


def test_render_tolerates_missing_band_phrase() -> None:
    # Band with no seeded phrase still yields a sentence (label + band + score).
    out = _catalog().render(code="trend_score", score=45.0, band="Moderate")
    assert out == "Demand Volume - Moderate (45/100)."


def test_helpers_read_the_catalog() -> None:
    cat = _catalog()
    assert cat.label("trend_score") == "Demand Volume"
    assert cat.meaning("mos") == "overall verdict"
    assert cat.band_phrase("mos", "Good Opportunity") == "A solid market worth pursuing"
    assert cat.band_phrase("trend_score", None) is None
