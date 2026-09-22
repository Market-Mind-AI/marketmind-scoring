"""Each call site asks Langfuse for its own instruction, by the name in the spec,
and sends the model what came back."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from langfuse.api import Prompt_Text
from langfuse.model import TextPromptClient

from app import observability, prompts
from app.services.catalog_matcher import CatalogMatchDecision, CatalogMatcher
from app.services.enrichment_client import EnrichmentClient, LocalizedMarketsResult
from app.services.explanation_client import _system_prompt
from app.services.reference_market_client import ReferenceCandidatesResult, ReferenceMarketClient
from app.services.vision_client import MarketTerms, ProductIdentification, VisionClient


class EditedInLangfuse:
    """Stands in for Langfuse: every prompt comes back as 'EDITED <name>'."""

    def __init__(self) -> None:
        self.names: list[str] = []

    def get_prompt(self, name: str, **_: object) -> TextPromptClient:
        self.names.append(name)
        return TextPromptClient(
            Prompt_Text(name=name, version=7, prompt=f"EDITED {name}", labels=["production"], tags=[], config={})
        )


@pytest.fixture
def langfuse(monkeypatch: pytest.MonkeyPatch) -> EditedInLangfuse:
    fake = EditedInLangfuse()
    monkeypatch.setattr(observability, "client", lambda: fake)
    yield fake
    prompts.take_linked_prompt()


class _Capture:
    def __init__(self, reply: object) -> None:
        self.reply = reply
        self.system: str | None = None
        self.user: str | None = None

    async def complete(self, *, system=None, user, image=None, response_model=None):
        self.system, self.user = system, user
        return self.reply


class _Product:
    def __init__(self, title: str) -> None:
        self.entity_key = uuid.uuid4()
        self.canonical_title = title
        self.category = None
        self.localized_markets = None


async def test_catalog_match_uses_its_prompt(langfuse: EditedInLangfuse) -> None:
    air = _Product("air fryer")
    provider = _Capture(CatalogMatchDecision(entity_key=str(air.entity_key), reasoning="same appliance"))

    await CatalogMatcher(provider=provider).pick(text="freidora de aire", candidates=[air])

    assert provider.system == "EDITED catalog-match-system"
    assert langfuse.names == ["catalog-match-system"]


async def test_enrichment_uses_its_prompt(langfuse: EditedInLangfuse) -> None:
    provider = AsyncMock()
    provider.complete = AsyncMock(
        return_value=LocalizedMarketsResult(
            markets=[MarketTerms(country="BRA", primary_name="fritadeira", search_terms=["fritadeira"], confidence=0.9)]
        )
    )
    with patch("app.services.enrichment_client.resolve_enrichment_llm", return_value=provider):
        client = EnrichmentClient()

    await client.localized_markets(product_name="air fryer", brand=None, category="kitchen", countries=[("BRA", "pt")])

    assert provider.complete.await_args.kwargs["system"] == "EDITED enrichment-system"


async def test_reference_market_uses_its_prompt(langfuse: EditedInLangfuse) -> None:
    provider = AsyncMock()
    provider.complete = AsyncMock(
        return_value=ReferenceCandidatesResult(
            candidates=[
                MarketTerms(country="BRA", primary_name="fritadeira", search_terms=["air fryer"], confidence=0.95),
                MarketTerms(country="USA", primary_name="air fryer", search_terms=["air fryer"], confidence=0.9),
            ]
        )
    )
    with patch("app.services.reference_market_client.resolve_reference_market_llm", return_value=provider):
        client = ReferenceMarketClient()

    await client.candidates(
        product_name="air fryer", brand=None, category="kitchen", supported_countries=["USA", "BRA", "DEU"]
    )

    assert provider.complete.await_args.kwargs["system"] == "EDITED reference-market-system"


async def test_vision_uses_its_prompt_and_keeps_the_users_text(langfuse: EditedInLangfuse) -> None:
    provider = AsyncMock()
    provider.complete = AsyncMock(
        return_value=ProductIdentification(
            product_name="air fryer",
            brand=None,
            category="kitchen",
            target_age_min=None,
            target_age_max=None,
            estimated_price=None,
            price_currency=None,
            confidence=0.9,
        )
    )
    with patch("app.services.vision_client.resolve_vision_llm", return_value=provider):
        await VisionClient().identify_product(
            image_base64=None, image_media_type=None, hint_text="air fryer 5L", countries=["BRA"]
        )

    assert provider.complete.await_args.kwargs["user"] == (
        "EDITED vision-instructions\n\nProduct description from the user: air fryer 5L"
    )


@pytest.mark.parametrize(
    ("language", "name"),
    [("pt", "explanation-system-pt"), ("en", "explanation-system-en"), ("es", "explanation-system-en")],
)
def test_explanation_uses_its_prompt_per_language(langfuse: EditedInLangfuse, language: str, name: str) -> None:
    rendered = _system_prompt(language, single_country=True)

    assert rendered.startswith(f"EDITED {name}")
    assert langfuse.names == [name]
