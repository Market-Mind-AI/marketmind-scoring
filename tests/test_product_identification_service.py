import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from app.schemas.catalog import CatalogResolveResponse, IdentifyImageRequest
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
                search_language="en",
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


async def test_identify_resolves_by_generic_name_and_passes_brand_apart(mock_session: AsyncMock) -> None:
    # Até 2026-08-25 este teste afirmava o contrário: que identify resolvia com
    # "Philips air fryer". Concatenar decidia identidade com um token que todos
    # os produtos da marca compartilham, e um purificador de ar Philips virou
    # air fryer no catálogo real. A marca agora vai separada, para ser
    # acumulada na entidade sem participar da decisão.
    # identify segue VISION-ONLY (rápido): mantém os termos da visão e ADIA
    # enrichment + reference candidates para o /analyze em background.
    entity_key = uuid.uuid4()
    vision = AsyncMock()
    vision.identify_product.return_value = _identification()
    enrichment, reference = _clients()
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
    assert call_args.args[0] == "air fryer", "identidade é o nome genérico"
    assert call_args.kwargs["brand"] == "Philips", "a marca vai separada, não concatenada"
    assert call_args.kwargs["localized_markets"]["USA"]["search_terms"] == ["air fryer", "airfryer"]  # from vision
    assert call_args.kwargs["reference_candidates"] is None  # deferred
    assert result.entity_key == entity_key
    assert result.reference_candidates is None
    # The slow LLM calls are NOT made at identify time.
    enrichment.localized_markets.assert_not_awaited()
    reference.candidates.assert_not_awaited()


async def test_identify_is_vision_only_defers_enrichment(mock_session: AsyncMock) -> None:
    # Vision returns USA only; requesting USA+BRA. identify does NOT enrich BRA
    # (that happens later in the background), so only the vision market is kept.
    vision = AsyncMock()
    vision.identify_product.return_value = _identification()
    enrichment, reference = _clients()
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

    enrichment.localized_markets.assert_not_awaited()
    assert result.localized_markets is not None
    assert set(result.localized_markets) == {"USA"}  # only vision's market; BRA deferred
    assert result.localized_markets["USA"].search_terms == ["air fryer", "airfryer"]


async def test_identify_text_only_no_image(mock_session: AsyncMock) -> None:
    # The image is optional: identify from the text description alone. The vision
    # client is called with image_base64=None and the provenance is text-sourced.
    vision = AsyncMock()
    vision.identify_product.return_value = _identification()
    enrichment, reference = _clients()
    catalog_cls, _country_cls, patchers = _patch_collaborators()
    try:
        service = ProductIdentificationService(
            mock_session, vision_client=vision, enrichment_client=enrichment, reference_market_client=reference
        )
        result = await service.identify(
            image_base64=None,
            image_media_type=None,
            hint_text="a Philips air fryer for the kitchen",
            countries=["USA"],
        )
    finally:
        for p in patchers:
            p.stop()

    assert vision.identify_product.call_args.kwargs["image_base64"] is None
    assert catalog_cls.return_value.resolve.call_args.kwargs["source"] == "text_identification"
    assert result.entity_key is not None


def test_identify_request_requires_image_or_text() -> None:
    # Neither image nor text → rejected.
    with pytest.raises(ValidationError):
        IdentifyImageRequest(countries=["BRA"])
    # Text only → OK (no image needed).
    IdentifyImageRequest(hint_text="a Philips air fryer", countries=["BRA"])
    # Image without a media type → rejected.
    with pytest.raises(ValidationError):
        IdentifyImageRequest(image_base64="ZmFrZQ==", countries=["BRA"])
    # Image + media type → OK.
    IdentifyImageRequest(image_base64="ZmFrZQ==", image_media_type="image/jpeg", countries=["BRA"])


async def test_identify_returns_no_markets_when_vision_finds_none(mock_session: AsyncMock) -> None:
    # Vision returns no markets → identify returns none (no deterministic fill
    # here; the background enrichment handles that at /analyze time).
    vision = AsyncMock()
    vision.identify_product.return_value = _identification(markets=[])
    enrichment, reference = _clients()
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

    enrichment.localized_markets.assert_not_awaited()
    assert result.localized_markets is None
    assert result.reference_candidates is None
