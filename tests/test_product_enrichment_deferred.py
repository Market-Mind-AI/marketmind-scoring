"""The enrichment deferred out of identify-image into AnalysisService runs in
the background and calls the LLM ONLY for what's missing — a country without
terms triggers enrichment for it; empty reference_candidates trigger the
candidates call. If the base already has everything, NO LLM call is made
(reuse → faster + cheaper). Reference candidates outside the supported basket
are dropped."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

_CTOR_DEPS = [
    "AnalysisRepository", "ProductRepository", "CountryRepository", "IndicatorRepository",
    "CoverageService", "IndicatorEngine", "MosEngine", "WeightCalibrationService",
    "ExplanationClient", "EnrichmentClient", "ReferenceMarketClient",
]  # fmt: skip


def _bare_service() -> tuple[Any, list[Any]]:
    patchers = [patch(f"app.services.analysis_service.{name}") for name in _CTOR_DEPS]
    for p in patchers:
        p.start()
    from app.services.analysis_service import AnalysisService

    return AnalysisService(AsyncMock()), patchers


def _market(country: str, term: str) -> SimpleNamespace:
    return SimpleNamespace(
        country=country, primary_name=term, formal_name=None,
        search_terms=[term], excluded_terms=[], confidence=0.9,
    )  # fmt: skip


def _entity(localized: dict[str, Any], candidates: list[dict[str, Any]] | None) -> SimpleNamespace:
    return SimpleNamespace(
        canonical_title="air fryer", category="kitchen",
        localized_markets=localized, reference_candidates=candidates,
    )  # fmt: skip


async def test_enrichment_called_only_for_missing_country() -> None:
    svc, patchers = _bare_service()
    try:
        svc._enrichment_client.localized_markets = AsyncMock(return_value=[_market("JPN", "ノンフライヤー")])
        svc._reference_market_client.candidates = AsyncMock(return_value=[])
        svc._country_repo.get_by_iso3 = AsyncMock(return_value=SimpleNamespace(search_language="ja"))
        entity = _entity(
            {"USA": {"primary_name": "air fryer", "search_terms": ["air fryer"], "excluded_terms": [], "confidence": 0.9}},
            candidates=[{"country": "BRA", "primary_name": "fritadeira", "search_terms": ["fritadeira"]}],
        )
        await svc._ensure_product_enrichment(entity, ["USA", "JPN"])
    finally:
        for p in patchers:
            p.stop()

    # JPN missing → enrichment asked ONLY for JPN (with its language); merged in.
    svc._enrichment_client.localized_markets.assert_awaited_once()
    assert svc._enrichment_client.localized_markets.await_args.kwargs["countries"] == [("JPN", "ja")]
    assert entity.localized_markets["JPN"]["search_terms"] == ["ノンフライヤー"]
    assert entity.localized_markets["USA"]["search_terms"] == ["air fryer"]  # untouched
    # reference_candidates already present → NOT regenerated.
    svc._reference_market_client.candidates.assert_not_awaited()


async def test_no_llm_call_when_base_already_complete() -> None:
    svc, patchers = _bare_service()
    try:
        svc._enrichment_client.localized_markets = AsyncMock()
        svc._reference_market_client.candidates = AsyncMock()
        entity = _entity(
            {"USA": {"primary_name": "air fryer", "search_terms": ["air fryer"], "excluded_terms": [], "confidence": 0.9}},
            candidates=[{"country": "BRA", "search_terms": ["fritadeira"]}],
        )
        await svc._ensure_product_enrichment(entity, ["USA"])
    finally:
        for p in patchers:
            p.stop()

    svc._enrichment_client.localized_markets.assert_not_awaited()  # nothing new → no cost
    svc._reference_market_client.candidates.assert_not_awaited()


async def test_reference_candidates_outside_basket_dropped() -> None:
    svc, patchers = _bare_service()
    try:
        svc._enrichment_client.localized_markets = AsyncMock(return_value=[])
        svc._country_repo.list_all = AsyncMock(return_value=[SimpleNamespace(iso3=c) for c in ("USA", "BRA")])
        svc._reference_market_client.candidates = AsyncMock(
            return_value=[_market("XXX", "x"), _market("BRA", "fritadeira")]
        )
        entity = _entity(
            {"USA": {"primary_name": "air fryer", "search_terms": ["air fryer"], "excluded_terms": [], "confidence": 0.9}},
            candidates=None,  # empty → candidates call runs
        )
        await svc._ensure_product_enrichment(entity, ["USA"])
    finally:
        for p in patchers:
            p.stop()

    assert [c["country"] for c in entity.reference_candidates] == ["BRA"]  # XXX dropped (outside basket)
