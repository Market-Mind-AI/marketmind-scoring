import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.schemas.catalog import CatalogResolveResponse
from app.services.enrichment_client import EnrichmentError
from app.services.product_identification_service import ProductIdentificationService
from app.services.vision_client import MarketTerms, ProductIdentification


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


async def test_identify_combines_brand_and_product_name_for_resolve(mock_session: AsyncMock) -> None:
    entity_key = uuid.uuid4()
    mock_vision_client = AsyncMock()
    mock_vision_client.identify_product.return_value = _identification()

    with patch("app.services.product_identification_service.CatalogService") as mock_catalog_service_cls:
        mock_catalog_service = mock_catalog_service_cls.return_value
        mock_catalog_service.resolve = AsyncMock(
            return_value=CatalogResolveResponse(
                entity_key=entity_key, canonical_title="Philips air fryer", method="created"
            )
        )

        service = ProductIdentificationService(mock_session, vision_client=mock_vision_client)
        result = await service.identify(
            image_base64="ZmFrZQ==", image_media_type="image/jpeg", hint_text=None, countries=["USA"]
        )

    mock_vision_client.identify_product.assert_awaited_once()
    assert mock_vision_client.identify_product.call_args.kwargs["countries"] == ["USA"]

    mock_catalog_service.resolve.assert_awaited_once()
    call_args = mock_catalog_service.resolve.call_args
    assert call_args.args[0] == "Philips air fryer"
    assert call_args.kwargs["category"] == "kitchen"
    # markets list mapped into the ISO3-keyed localized_markets dict, both for
    # persistence (resolve) and the response.
    expected_markets = {
        "USA": {
            "primary_name": "air fryer",
            "formal_name": None,
            "search_terms": ["air fryer", "airfryer"],
            "excluded_terms": ["deep fryer"],
            "confidence": 0.95,
        }
    }
    assert call_args.kwargs["localized_markets"] == expected_markets

    assert result.entity_key == entity_key
    assert result.product_name == "air fryer"
    assert result.brand == "Philips"
    assert result.estimated_price == 89.9
    assert result.localized_markets is not None
    assert result.localized_markets["USA"].search_terms == ["air fryer", "airfryer"]


async def test_identify_uses_product_name_only_when_no_brand(mock_session: AsyncMock) -> None:
    mock_vision_client = AsyncMock()
    mock_vision_client.identify_product.return_value = _identification(brand=None)

    with patch("app.services.product_identification_service.CatalogService") as mock_catalog_service_cls:
        mock_catalog_service = mock_catalog_service_cls.return_value
        mock_catalog_service.resolve = AsyncMock(
            return_value=CatalogResolveResponse(entity_key=uuid.uuid4(), canonical_title="air fryer", method="created")
        )

        service = ProductIdentificationService(mock_session, vision_client=mock_vision_client)
        await service.identify(
            image_base64="ZmFrZQ==", image_media_type="image/jpeg", hint_text=None, countries=["USA"]
        )

    assert mock_catalog_service.resolve.call_args.args[0] == "air fryer"


async def test_enrichment_fills_markets_vision_missed(mock_session: AsyncMock) -> None:
    # Vision returns USA only; the enrichment LLM fills the missing BRA so
    # localized_markets covers every requested country.
    mock_vision_client = AsyncMock()
    mock_vision_client.identify_product.return_value = _identification()  # USA only

    mock_enrichment = AsyncMock()
    mock_enrichment.localized_markets = AsyncMock(
        return_value=[
            MarketTerms(
                country="BRA",
                primary_name="fritadeira sem óleo",
                search_terms=["fritadeira sem óleo", "airfryer"],
                excluded_terms=[],
                confidence=0.9,
            )
        ]
    )

    with (
        patch("app.services.product_identification_service.CatalogService") as mock_catalog_service_cls,
        patch("app.services.product_identification_service.CountryRepository") as mock_country_repo_cls,
    ):
        mock_catalog_service_cls.return_value.resolve = AsyncMock(
            return_value=CatalogResolveResponse(
                entity_key=uuid.uuid4(), canonical_title="Philips air fryer", method="created"
            )
        )
        mock_country_repo_cls.return_value.get_by_iso3 = AsyncMock(return_value=SimpleNamespace(search_language="pt"))
        service = ProductIdentificationService(
            mock_session, vision_client=mock_vision_client, enrichment_client=mock_enrichment
        )
        result = await service.identify(
            image_base64="ZmFrZQ==", image_media_type="image/jpeg", hint_text=None, countries=["USA", "BRA"]
        )

    # Enrichment was asked for exactly the missing country (with its language).
    assert mock_enrichment.localized_markets.await_args.kwargs["countries"] == [("BRA", "pt")]
    assert result.localized_markets is not None
    assert result.localized_markets["USA"].search_terms == ["air fryer", "airfryer"]  # kept from vision
    assert result.localized_markets["BRA"].search_terms == ["fritadeira sem óleo", "airfryer"]  # from enrichment


async def test_fallback_when_enrichment_fails_so_markets_never_empty(mock_session: AsyncMock) -> None:
    # Vision returns no markets AND the enrichment LLM errors — localized_markets
    # must still never be empty: a deterministic fallback fills every country.
    mock_vision_client = AsyncMock()
    mock_vision_client.identify_product.return_value = _identification(markets=[])

    mock_enrichment = AsyncMock()
    mock_enrichment.localized_markets = AsyncMock(side_effect=EnrichmentError("model down"))

    with (
        patch("app.services.product_identification_service.CatalogService") as mock_catalog_service_cls,
        patch("app.services.product_identification_service.CountryRepository") as mock_country_repo_cls,
    ):
        mock_catalog_service_cls.return_value.resolve = AsyncMock(
            return_value=CatalogResolveResponse(
                entity_key=uuid.uuid4(), canonical_title="Philips air fryer", method="created"
            )
        )
        mock_country_repo_cls.return_value.get_by_iso3 = AsyncMock(return_value=SimpleNamespace(search_language="en"))
        service = ProductIdentificationService(
            mock_session, vision_client=mock_vision_client, enrichment_client=mock_enrichment
        )
        result = await service.identify(
            image_base64="ZmFrZQ==", image_media_type="image/jpeg", hint_text=None, countries=["USA"]
        )

    assert result.localized_markets is not None
    usa = result.localized_markets["USA"]
    # Deterministic fallback: the product's own name/brand as the search term.
    assert usa.search_terms  # never empty
    assert "Philips air fryer" in usa.search_terms
    assert usa.confidence == 0.3
