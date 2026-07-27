"""Unit tests for the demand-confidence guard (_flag_demand_outliers) — the
automated stand-in for human term curation: it flags a DEVELOPED market whose
demand penetration is a gross low outlier (the tell-tale of a missing native
search term, e.g. Japan without ノンフライヤー / Korea's Naver share), while NOT
false-flagging genuinely low-penetration low-income markets."""

from typing import Any

from app.services.explanation_client import _flag_demand_outliers


def _country(iso3: str, penetration: float, gdp_per_capita_score: float) -> dict[str, Any]:
    return {
        "country": iso3,
        "signals": [
            {
                "code": "trend_score",
                "context": f"1,000 total monthly searches; population 1; "
                f"penetration {penetration} searches per 100k inhabitants (share...)",
            },
            {"code": "gdp_per_capita_score", "score": gdp_per_capita_score},
        ],
    }


def test_flags_developed_low_penetration_but_not_low_income() -> None:
    countries = [
        _country("DEU", 1000, 50.0),
        _country("USA", 400, 75.0),
        _country("FRA", 1100, 40.0),
        _country("JPN", 40, 29.0),  # developed + gross-low penetration → FLAG
        _country("IND", 30, 1.4),  # low-income + low penetration → expected, NOT flagged
    ]
    _flag_demand_outliers(countries)
    conf = {c["country"]: c["demand_confidence"] for c in countries}
    assert conf["JPN"] == "low"
    assert "demand_confidence_note" in next(c for c in countries if c["country"] == "JPN")
    assert conf["IND"] == "ok"  # gdp gate spares the low-income market
    assert conf["DEU"] == "ok" and conf["USA"] == "ok" and conf["FRA"] == "ok"


def test_no_flag_without_enough_peers() -> None:
    countries = [_country("USA", 400, 75.0), _country("JPN", 10, 29.0)]
    _flag_demand_outliers(countries)
    assert all(c["demand_confidence"] == "ok" for c in countries)  # < 3 peers → no median


def test_all_similar_no_flag() -> None:
    countries = [_country(c, 500, 50.0) for c in ("USA", "DEU", "FRA", "ITA")]
    _flag_demand_outliers(countries)
    assert all(c["demand_confidence"] == "ok" for c in countries)
