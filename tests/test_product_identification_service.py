import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from app.schemas.catalog import CatalogResolveResponse, IdentifyImageRequest
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


def _patch_collaborators(entity_key: uuid.UUID | None = None):
    """Patches the CatalogService the service constructs."""
    entity_key = entity_key or uuid.uuid4()
    catalog = patch("app.services.product_identification_service.CatalogService")
    catalog_cls = catalog.start()
    catalog_cls.return_value.resolve = AsyncMock(
        return_value=CatalogResolveResponse(
            entity_key=entity_key, canonical_title="Philips air fryer", method="created"
        )
    )
    # Catálogo vazio: nem termo de busca igual nem candidato — o fluxo completo.
    catalog_cls.return_value.candidatos_para_identificacao = AsyncMock(return_value=[])
    repo = patch("app.services.product_identification_service.ProductRepository")
    repo.start().return_value.owners_of_search_term = AsyncMock(return_value=set())
    return catalog_cls, [catalog, repo]


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
    catalog_cls, patchers = _patch_collaborators(entity_key)
    try:
        service = ProductIdentificationService(mock_session, vision_client=vision)
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
    # The slow LLM calls (enrichment, reference markets) have no client here at
    # all: they live in AnalysisService and run in the /analyze background task.


async def test_identify_hands_attributes_and_antagonists_to_the_catalog(mock_session: AsyncMock) -> None:
    # O enriquecimento roda depois, no /analyze, e só tem a entidade: o que a
    # identificação disse sobre o produto precisa ficar gravado nela.
    vision = AsyncMock()
    vision.identify_product.return_value = _identification(
        defining_attributes=["cooks with hot air"], antagonist_products=["oil deep fryer"]
    )
    catalog_cls, patchers = _patch_collaborators()
    try:
        service = ProductIdentificationService(mock_session, vision_client=vision)
        await service.identify(image_base64=None, image_media_type=None, hint_text="air fryer", countries=["USA"])
    finally:
        for p in patchers:
            p.stop()

    kwargs = catalog_cls.return_value.resolve.call_args.kwargs
    assert (kwargs["estimated_price"], kwargs["price_currency"]) == (89.9, "USD"), "gravado para a próxima vez"
    assert kwargs["defining_attributes"] == ["cooks with hot air"]
    assert kwargs["antagonist_products"] == ["oil deep fryer"]


async def test_identify_is_vision_only_defers_enrichment(mock_session: AsyncMock) -> None:
    # Vision returns USA only; requesting USA+BRA. identify does NOT enrich BRA
    # (that happens later in the background), so only the vision market is kept.
    vision = AsyncMock()
    vision.identify_product.return_value = _identification()
    _catalog_cls, patchers = _patch_collaborators()
    try:
        service = ProductIdentificationService(mock_session, vision_client=vision)
        result = await service.identify(
            image_base64="ZmFrZQ==", image_media_type="image/jpeg", hint_text=None, countries=["USA", "BRA"]
        )
    finally:
        for p in patchers:
            p.stop()

    assert result.localized_markets is not None
    assert set(result.localized_markets) == {"USA"}  # only vision's market; BRA deferred
    assert result.localized_markets["USA"].search_terms == ["air fryer", "airfryer"]


async def test_identify_text_only_no_image(mock_session: AsyncMock) -> None:
    # The image is optional: identify from the text description alone. The vision
    # client is called with image_base64=None and the provenance is text-sourced.
    vision = AsyncMock()
    vision.identify_product.return_value = _identification()
    catalog_cls, patchers = _patch_collaborators()
    try:
        service = ProductIdentificationService(mock_session, vision_client=vision)
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
    _catalog_cls, patchers = _patch_collaborators()
    try:
        service = ProductIdentificationService(mock_session, vision_client=vision)
        result = await service.identify(
            image_base64="ZmFrZQ==", image_media_type="image/jpeg", hint_text=None, countries=["USA"]
        )
    finally:
        for p in patchers:
            p.stop()

    assert result.localized_markets is None
    assert result.reference_candidates is None


# --- o catálogo antes do modelo (2026-10-26) ----------------------------------
#
# Cada identificação adivinhava o produto do zero, e a mesma impressora voltava
# com 20–55, 18–65 e 20–60 anos de público: a nota oscilava sem dado novo.


def _gravado():  # noqa: ANN202
    from types import SimpleNamespace

    return SimpleNamespace(
        entity_key=uuid.uuid4(),
        canonical_title="U1 Multicolor 3D Printer",
        category="3D Printers",
        target_age_min=20,
        target_age_max=55,
        estimated_price=Decimal("999"),
        price_currency="EUR",
    )


def _com_catalogo(*, donos: set, candidatos: list):  # noqa: ANN202
    from types import SimpleNamespace

    entidade = _gravado()
    catalogo = patch("app.services.product_identification_service.CatalogService")
    catalogo.start().return_value.candidatos_para_identificacao = AsyncMock(
        return_value=[(entidade if c is None else c, "Candidate text") for c in candidatos]
    )
    repo = patch("app.services.product_identification_service.ProductRepository")
    repo_mock = repo.start().return_value
    repo_mock.owners_of_search_term = AsyncMock(return_value=donos)
    repo_mock.get_by_entity_key = AsyncMock(return_value=entidade)
    repo_mock.get_brand = AsyncMock(return_value=SimpleNamespace(name="Snapmaker", brand_key=uuid.uuid4()))
    repo_mock.get_or_create_brand = AsyncMock(return_value=SimpleNamespace(name="Snapmaker", brand_key=uuid.uuid4()))
    repo_mock.markets_for = AsyncMock(
        return_value={"ESP": {"primary_name": "snapmaker u1", "search_terms": ["snapmaker u1"], "confidence": 0.9}}
    )
    return entidade, repo_mock, [catalogo, repo]


async def test_a_search_term_of_one_product_is_answered_without_any_model(mock_session: AsyncMock) -> None:
    entidade, _, patchers = _com_catalogo(donos={(uuid.uuid4(), uuid.uuid4())}, candidatos=[])
    vision = AsyncMock()
    try:
        result = await ProductIdentificationService(mock_session, vision_client=vision).identify(
            image_base64=None, image_media_type=None, hint_text="snapmaker u1", countries=["ESP"]
        )
    finally:
        for p in patchers:
            p.stop()

    vision.identify_product.assert_not_awaited()
    assert result.method == "lexical"
    assert (result.product_name, result.brand) == (entidade.canonical_title, "Snapmaker")
    assert (result.target_age_min, result.target_age_max) == (20, 55), "a faixa gravada, não um chute novo"
    assert (result.estimated_price, result.price_currency) == (999.0, "EUR")
    assert result.localized_markets["ESP"].search_terms == ["snapmaker u1"]


async def test_a_term_two_products_declare_is_not_a_lexical_answer(mock_session: AsyncMock) -> None:
    _, _, patchers = _com_catalogo(donos={(uuid.uuid4(), None), (uuid.uuid4(), None)}, candidatos=[])
    catalog = patch("app.services.product_identification_service.CatalogService")
    catalog_cls = catalog.start()
    catalog_cls.return_value.candidatos_para_identificacao = AsyncMock(return_value=[])
    catalog_cls.return_value.resolve = AsyncMock(
        return_value=CatalogResolveResponse(entity_key=uuid.uuid4(), canonical_title="air fryer", method="created")
    )
    vision = AsyncMock()
    vision.identify_product.return_value = _identification()
    try:
        result = await ProductIdentificationService(mock_session, vision_client=vision).identify(
            image_base64=None, image_media_type=None, hint_text="air fryer", countries=["USA"]
        )
    finally:
        for p in [catalog, *patchers]:
            p.stop()

    vision.identify_product.assert_awaited_once()
    assert result.method == "created"


async def test_a_candidate_the_model_recognises_comes_back_as_stored(mock_session: AsyncMock) -> None:
    entidade, repo_mock, patchers = _com_catalogo(donos=set(), candidatos=[None])
    vision = AsyncMock()
    vision.identify_product.return_value = _identification(
        product_name="", catalog_match=1, brand="Snapmaker", target_age_min=18, target_age_max=65
    )
    try:
        result = await ProductIdentificationService(mock_session, vision_client=vision).identify(
            image_base64=None, image_media_type=None, hint_text="impresora snapmaker u1", countries=["ESP"]
        )
    finally:
        for p in patchers:
            p.stop()

    assert vision.identify_product.await_args.kwargs["candidates"] == ["Candidate text"]
    assert result.method == "catalog"
    assert result.entity_key == entidade.entity_key
    assert (result.target_age_min, result.target_age_max) == (20, 55), "o modelo só diz a marca"
    repo_mock.get_or_create_brand.assert_awaited_once()


async def test_a_photo_of_a_known_product_comes_back_as_stored(mock_session: AsyncMock) -> None:
    # Só a foto: não há texto para buscar candidatos, quem reconhece é o
    # resolve. O chute do modelo não pode voltar para a tela de revisão — o
    # /analyze o tomava por edição do usuário e mudava o catálogo.
    entidade, repo_mock, patchers = _com_catalogo(donos=set(), candidatos=[])
    catalog = patch("app.services.product_identification_service.CatalogService")
    catalog_cls = catalog.start()
    catalog_cls.return_value.resolve = AsyncMock(
        return_value=CatalogResolveResponse(
            entity_key=entidade.entity_key,
            canonical_title=entidade.canonical_title,
            method="alias",
            brand_key=uuid.uuid4(),
        )
    )
    vision = AsyncMock()
    vision.identify_product.return_value = _identification(
        product_name="U1 3D Printer", brand="Snapmaker", target_age_min=14, target_age_max=60
    )
    try:
        result = await ProductIdentificationService(mock_session, vision_client=vision).identify(
            image_base64="aGVsbG8=", image_media_type="image/png", hint_text=None, countries=["ESP"]
        )
    finally:
        for p in [catalog, *patchers]:
            p.stop()

    assert result.method == "alias"
    assert (result.product_name, result.brand) == (entidade.canonical_title, "Snapmaker")
    assert (result.target_age_min, result.target_age_max) == (20, 55), "a faixa gravada, não a da foto"
    assert (result.estimated_price, result.price_currency) == (999.0, "EUR")
    repo_mock.get_brand.assert_awaited_once()
