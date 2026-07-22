import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.schemas.catalog import CatalogResolveResponse
from app.services.enrichment_client import EnrichmentError
from app.services.product_identification_service import ProductIdentificationService
from app.services.vision_client import MarketTerms, ProductIdentification

_SUPPORTED = ["USA", "BRA", "DEU", "JPN"]


def _identification(**overrides: object) -> ProductIdentification:
    base = {
        "product_name": "air fryer",
        "brand": "Philips",
        "category": "kitchen",
        "markets": [
            MarketTerms(
                country="USA",
                primary_name="air fryer",
                search_terms=["air fryer", "airfryer"],
                excluded_terms=["deep fryer"],
                confidence=0.95,
            )
        ],
        "target_age_min": None,
        "target_age_max": None,
        "estimated_price": 89.9,
        "price_currency": "USD",
        "confidence": 0.9,
    }
    base.update(overrides)
    return ProductIdentification(**base)  # type: ignore[arg-type]


def _clients(*, fill: list[MarketTerms] | None = None, candidates: list[MarketTerms] | None = None):
    enrichment = AsyncMock()
    enrichment.localized_markets = AsyncMock(return_value=fill or [])
    reference = AsyncMock()
    reference.candidates = AsyncMock(return_value=candidates or [])
    return enrichment, reference


def _patch_collaborators(entity_key: uuid.UUID | None = None):
    """Patches CatalogService + CountryRepository the service constructs."""
    entity_key = entity_key or uuid.uuid4()
    catalog = patch("app.services.product_identification_service.CatalogService")
    country = patch("app.services.product_identification_service.CountryRepository")
    catalog_cls = catalog.start()
    country_cls = country.start()
    catalog_cls.return_value.resolve = AsyncMock(
        return_value=CatalogResolveResponse(
            entity_key=entity_key, canonical_title="Philips air fryer", method="created"
        )
    )
    country_cls.return_value.list_all = AsyncMock(return_value=[SimpleNamespace(iso3=c) for c in _SUPPORTED])
    country_cls.return_value.get_by_iso3 = AsyncMock(return_value=SimpleNamespace(search_language="pt"))
    return catalog_cls, country_cls, [catalog, country]


async def test_identify_combines_brand_and_product_name_for_resolve(mock_session: AsyncMock) -> None:
    entity_key = uuid.uuid4()
    vision = AsyncMock()
    vision.identify_product.return_value = _identification()
    enrichment, reference = _clients(
        candidates=[
            MarketTerms(country="USA", primary_name="air fryer", search_terms=["air fryer"], confidence=0.95),
            MarketTerms(country="BRA", primary_name="fritadeira", search_terms=["fritadeira"], confidence=0.9),
        ]
    )
    catalog_cls, _country_cls, patchers = _patch_collaborators(entity_key)
    try:
        service = ProductIdentificationService(
            mock_session, vision_client=vision, enrichment_client=enrichment, reference_market_client=reference
        )
        result = await service.identify(
            image_base64="ZmFrZQ==", image_media_type="image/jpeg", hint_text=None, countries=["USA"]
        )
    finally:
        for p in patchers:
            p.stop()

    call_args = catalog_cls.return_value.resolve.call_args
    assert call_args.args[0] == "Philips air fryer"
    assert call_args.kwargs["localized_markets"]["USA"]["search_terms"] == ["air fryer", "airfryer"]
    # Candidates (from the real-LLM client) are persisted + returned, ranked, each with country.
    assert [c["country"] for c in call_args.kwargs["reference_candidates"]] == ["USA", "BRA"]
    assert result.entity_key == entity_key
    assert result.reference_candidates is not None
    assert [c.country for c in result.reference_candidates] == ["USA", "BRA"]
    # The reference-market client was offered the supported basket.
    assert reference.candidates.await_args.kwargs["supported_countries"] == _SUPPORTED


async def test_enrichment_fills_markets_vision_missed(mock_session: AsyncMock) -> None:
    # Vision returns USA only; the enrichment (local) client fills the missing BRA.
    vision = AsyncMock()
    vision.identify_product.return_value = _identification()
    enrichment, reference = _clients(
        fill=[
            MarketTerms(
                country="BRA", primary_name="fritadeira sem óleo", search_terms=["fritadeira sem óleo"], confidence=0.9
            )
        ],
        candidates=[MarketTerms(country="USA", primary_name="air fryer", search_terms=["air fryer"], confidence=0.95)],
    )
    _catalog_cls, _country_cls, patchers = _patch_collaborators()
    try:
        service = ProductIdentificationService(
            mock_session, vision_client=vision, enrichment_client=enrichment, reference_market_client=reference
        )
        result = await service.identify(
            image_base64="ZmFrZQ==", image_media_type="image/jpeg", hint_text=None, countries=["USA", "BRA"]
        )
    finally:
        for p in patchers:
            p.stop()

    # Enrichment asked to fill only the missing BRA (with its language).
    assert enrichment.localized_markets.await_args.kwargs["countries"] == [("BRA", "pt")]
    assert result.localized_markets is not None
    assert result.localized_markets["USA"].search_terms == ["air fryer", "airfryer"]  # kept from vision
    assert result.localized_markets["BRA"].search_terms == ["fritadeira sem óleo"]  # from enrichment


async def test_fallback_when_enrichment_fails_so_markets_never_empty(mock_session: AsyncMock) -> None:
    # Vision returns no markets AND enrichment errors — localized_markets still
    # never empty (deterministic fallback); no reference candidates.
    vision = AsyncMock()
    vision.identify_product.return_value = _identification(markets=[])
    enrichment, reference = _clients()
    enrichment.localized_markets = AsyncMock(side_effect=EnrichmentError("model down"))
    _catalog_cls, _country_cls, patchers = _patch_collaborators()
    try:
        service = ProductIdentificationService(
            mock_session, vision_client=vision, enrichment_client=enrichment, reference_market_client=reference
        )
        result = await service.identify(
            image_base64="ZmFrZQ==", image_media_type="image/jpeg", hint_text=None, countries=["USA"]
        )
    finally:
        for p in patchers:
            p.stop()

    assert result.localized_markets is not None
    usa = result.localized_markets["USA"]
    assert "Philips air fryer" in usa.search_terms  # deterministic fallback
    assert usa.confidence == 0.3
    assert result.reference_candidates is None


async def test_candidates_outside_supported_basket_are_dropped(mock_session: AsyncMock) -> None:
    vision = AsyncMock()
    vision.identify_product.return_value = _identification()
    _enrichment, reference = _clients(
        candidates=[
            MarketTerms(country="XXX", primary_name="x", search_terms=["x"], confidence=0.9),
            MarketTerms(country="BRA", primary_name="fritadeira", search_terms=["fritadeira"], confidence=0.9),
        ]
    )
    enrichment, _ = _clients()
    _catalog_cls, _country_cls, patchers = _patch_collaborators()
    try:
        service = ProductIdentificationService(
            mock_session, vision_client=vision, enrichment_client=enrichment, reference_market_client=reference
        )
        result = await service.identify(
            image_base64="ZmFrZQ==", image_media_type="image/jpeg", hint_text=None, countries=["USA"]
        )
    finally:
        for p in patchers:
            p.stop()

    assert result.reference_candidates is not None
    assert [c.country for c in result.reference_candidates] == ["BRA"]  # XXX dropped
