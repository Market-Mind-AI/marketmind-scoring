from unittest.mock import AsyncMock, patch

import pytest

from app.services.enrichment_client import EnrichmentClient, EnrichmentError, LocalizedMarketsResult
from app.services.llm import LLMError
from app.services.vision_client import MarketTerms


def _client(provider: AsyncMock) -> EnrichmentClient:
    with patch("app.services.enrichment_client.resolve_enrichment_llm", return_value=provider):
        return EnrichmentClient()


async def test_localized_markets_returns_structured_markets() -> None:
    provider = AsyncMock()
    provider.complete = AsyncMock(
        return_value=LocalizedMarketsResult(
            markets=[MarketTerms(country="BRA", primary_name="fritadeira", search_terms=["fritadeira"], confidence=0.9)]
        )
    )
    client = _client(provider)
    markets = await client.localized_markets(
        product_name="air fryer", brand=None, category="kitchen", countries=[("BRA", "pt")]
    )

    assert [m.country for m in markets] == ["BRA"]
    # The structured schema was requested from the provider.
    assert provider.complete.await_args.kwargs["response_model"] is LocalizedMarketsResult


async def test_empty_countries_skips_the_call() -> None:
    provider = AsyncMock()
    provider.complete = AsyncMock()
    client = _client(provider)
    assert await client.localized_markets(product_name="x", brand=None, category=None, countries=[]) == []
    provider.complete.assert_not_awaited()


async def test_provider_error_becomes_enrichment_error() -> None:
    provider = AsyncMock()
    provider.complete = AsyncMock(side_effect=LLMError("model unavailable"))
    client = _client(provider)
    with pytest.raises(EnrichmentError, match="Enrichment LLM call failed"):
        await client.localized_markets(product_name="air fryer", brand=None, category=None, countries=[("USA", "en")])
