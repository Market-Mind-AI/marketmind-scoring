from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from anthropic import APIConnectionError

from app.core.config import settings
from app.services.vision_client import ProductIdentification, VisionClient, VisionError


def _mock_anthropic_client(parsed_output: ProductIdentification | None, stop_reason: str = "end_turn") -> AsyncMock:
    client = AsyncMock()
    client.messages.parse = AsyncMock(
        return_value=SimpleNamespace(parsed_output=parsed_output, stop_reason=stop_reason)
    )
    return client


async def test_identify_product_returns_parsed_output() -> None:
    identification = ProductIdentification(
        product_name="air fryer",
        brand="Philips",
        category="kitchen",
        target_age_min=None,
        target_age_max=None,
        estimated_price=89.9,
        price_currency="USD",
        confidence=0.9,
    )
    with (
        patch.object(settings, "anthropic_api_key", "test-key"),
        patch("app.services.vision_client.AsyncAnthropic", return_value=_mock_anthropic_client(identification)),
    ):
        client = VisionClient()
        result = await client.identify_product(
            image_base64="ZmFrZQ==", image_media_type="image/jpeg", hint_text=None, countries=["USA"]
        )

    assert result.product_name == "air fryer"
    assert result.brand == "Philips"
    assert result.estimated_price == 89.9


async def test_identify_product_raises_without_api_key() -> None:
    with patch.object(settings, "anthropic_api_key", ""):
        client = VisionClient()
        with pytest.raises(VisionError, match="ANTHROPIC_API_KEY is not configured"):
            await client.identify_product(
                image_base64="ZmFrZQ==", image_media_type="image/jpeg", hint_text=None, countries=["USA"]
            )


async def test_identify_product_wraps_api_errors() -> None:
    fake_request = SimpleNamespace(method="POST", url="https://api.anthropic.com/v1/messages")
    mock_client = AsyncMock()
    mock_client.messages.parse = AsyncMock(side_effect=APIConnectionError(request=fake_request))  # type: ignore[arg-type]
    with (
        patch.object(settings, "anthropic_api_key", "test-key"),
        patch("app.services.vision_client.AsyncAnthropic", return_value=mock_client),
    ):
        client = VisionClient()
        with pytest.raises(VisionError, match="Failed to call Anthropic vision API"):
            await client.identify_product(
                image_base64="ZmFrZQ==", image_media_type="image/jpeg", hint_text=None, countries=["USA"]
            )


async def test_identify_product_raises_when_no_parsed_output() -> None:
    with (
        patch.object(settings, "anthropic_api_key", "test-key"),
        patch(
            "app.services.vision_client.AsyncAnthropic",
            return_value=_mock_anthropic_client(None, stop_reason="max_tokens"),
        ),
    ):
        client = VisionClient()
        with pytest.raises(VisionError, match="returned no parseable result"):
            await client.identify_product(
                image_base64="ZmFrZQ==", image_media_type="image/jpeg", hint_text=None, countries=["USA"]
            )
