from unittest.mock import AsyncMock, patch

import pytest

from app.services.llm import LLMError
from app.services.reference_market_client import (
    ReferenceCandidatesResult,
    ReferenceMarketClient,
    ReferenceMarketError,
)
from app.services.vision_client import MarketTerms


def _client(provider: AsyncMock) -> ReferenceMarketClient:
    with patch("app.services.reference_market_client.resolve_reference_market_llm", return_value=provider):
        return ReferenceMarketClient()


async def test_candidates_returns_ranked_shortlist() -> None:
    provider = AsyncMock()
    provider.complete = AsyncMock(
        return_value=ReferenceCandidatesResult(
            candidates=[
                MarketTerms(country="BRA", primary_name="fritadeira", search_terms=["air fryer"], confidence=0.95),
                MarketTerms(country="USA", primary_name="air fryer", search_terms=["air fryer"], confidence=0.9),
            ]
        )
    )
    client = _client(provider)
    cands = await client.candidates(
        product_name="air fryer", brand=None, category="kitchen", supported_countries=["USA", "BRA", "DEU"]
    )

    assert [c.country for c in cands] == ["BRA", "USA"]
    assert provider.complete.await_args.kwargs["response_model"] is ReferenceCandidatesResult
    assert "USA, BRA, DEU" in provider.complete.await_args.kwargs["user"]


async def test_empty_supported_skips_the_call() -> None:
    provider = AsyncMock()
    provider.complete = AsyncMock()
    client = _client(provider)
    assert await client.candidates(product_name="x", brand=None, category=None, supported_countries=[]) == []
    provider.complete.assert_not_awaited()


async def test_provider_error_becomes_reference_market_error() -> None:
    provider = AsyncMock()
    provider.complete = AsyncMock(side_effect=LLMError("model unavailable"))
    client = _client(provider)
    with pytest.raises(ReferenceMarketError, match="Reference-market LLM call failed"):
        await client.candidates(product_name="air fryer", brand=None, category=None, supported_countries=["USA"])
