from unittest.mock import AsyncMock, patch

import pytest

from app.services.llm import LLMError
from app.services.vision_client import (
    ProductIdentification,
    ProductNotIdentifiedError,
    VisionClient,
    VisionError,
)


def _mock_provider(*, returns: object = None, raises: Exception | None = None) -> AsyncMock:
    """A stand-in LLM provider whose async `complete` returns/raises as told."""
    provider = AsyncMock()
    provider.complete = AsyncMock(return_value=returns, side_effect=raises)
    return provider


def _identification() -> ProductIdentification:
    return ProductIdentification(
        product_name="air fryer",
        brand="Philips",
        category="kitchen",
        target_age_min=None,
        target_age_max=None,
        estimated_price=89.9,
        price_currency="USD",
        confidence=0.9,
    )


async def test_identify_product_returns_result() -> None:
    # VisionClient delegates to the resolved provider (default ollama /
    # qwen3-vl:8b-instruct) and returns its structured result.
    provider = _mock_provider(returns=_identification())
    with patch("app.services.vision_client.resolve_vision_llm", return_value=provider):
        client = VisionClient()
        result = await client.identify_product(
            image_base64="ZmFrZQ==", image_media_type="image/jpeg", hint_text=None, countries=["USA"]
        )

    assert result.product_name == "air fryer"
    assert result.estimated_price == 89.9
    # The image + structured schema were passed to the provider.
    _, kwargs = provider.complete.call_args
    assert kwargs["response_model"] is ProductIdentification
    assert kwargs["image"].base64_data == "ZmFrZQ=="


async def test_identify_product_rejects_hallucination_when_no_product_found() -> None:
    # Text like "quero" names no product: the model reports product_found=false
    # and must NOT be turned into a (hallucinated) result — identify raises.
    not_found = ProductIdentification(
        product_found=False,
        product_name="",
        brand=None,
        category=None,
        target_age_min=None,
        target_age_max=None,
        estimated_price=None,
        price_currency=None,
        confidence=0.0,
    )
    provider = _mock_provider(returns=not_found)
    with patch("app.services.vision_client.resolve_vision_llm", return_value=provider):
        client = VisionClient()
        with pytest.raises(ProductNotIdentifiedError):
            await client.identify_product(
                image_base64=None, image_media_type=None, hint_text="quero", countries=["BRA"]
            )


async def test_identify_product_text_only_passes_no_image() -> None:
    provider = _mock_provider(returns=_identification())
    with patch("app.services.vision_client.resolve_vision_llm", return_value=provider):
        client = VisionClient()
        result = await client.identify_product(
            image_base64=None, image_media_type=None, hint_text="a Philips air fryer", countries=["USA"]
        )
    assert result.product_name == "air fryer"
    _, kwargs = provider.complete.call_args
    assert kwargs["image"] is None  # text-only → no image sent


async def test_identify_product_wraps_provider_errors() -> None:
    provider = _mock_provider(raises=LLMError("model unavailable"))
    with patch("app.services.vision_client.resolve_vision_llm", return_value=provider):
        client = VisionClient()
        with pytest.raises(VisionError, match="Vision LLM call failed"):
            await client.identify_product(
                image_base64="ZmFrZQ==", image_media_type="image/jpeg", hint_text=None, countries=["USA"]
            )


async def test_identify_product_rejects_unexpected_result_type() -> None:
    # If a provider ever returns free text instead of the structured model.
    provider = _mock_provider(returns="not a ProductIdentification")
    with patch("app.services.vision_client.resolve_vision_llm", return_value=provider):
        client = VisionClient()
        with pytest.raises(VisionError, match="unexpected result type"):
            await client.identify_product(
                image_base64="ZmFrZQ==", image_media_type="image/jpeg", hint_text=None, countries=["USA"]
            )
