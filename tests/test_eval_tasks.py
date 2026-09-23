"""Each task runs the real client, so an experiment measures the code that ships."""

from unittest.mock import AsyncMock, patch

from app.services.reference_market_client import ReferenceCandidatesResult
from app.services.vision_client import MarketTerms, ProductIdentification
from evaluation import tasks


def _identification(**overrides) -> ProductIdentification:
    fields = {
        "product_found": True,
        "product_name": "air fryer",
        "brand": None,
        "category": "Kitchen Appliances",
        "target_age_min": None,
        "target_age_max": None,
        "estimated_price": None,
        "price_currency": None,
        "confidence": 0.9,
    }
    fields.update(overrides)
    return ProductIdentification(**fields)


async def test_identification_passes_the_case_to_the_real_client() -> None:
    provider = AsyncMock()
    provider.complete = AsyncMock(return_value=_identification())

    with patch("app.services.vision_client.resolve_vision_llm", return_value=provider):
        result = await tasks.identify_product(
            item={"input": {"hint_text": "air fryer 5.5L", "countries": ["BRA"]}}
        )

    assert result == {
        "product_found": True,
        "product_name": "air fryer",
        "category": "Kitchen Appliances",
        "brand": None,
    }
    kwargs = provider.complete.await_args.kwargs
    assert "air fryer 5.5L" in kwargs["user"]
    assert kwargs["image"] is None


async def test_a_refused_identification_is_reported_not_raised() -> None:
    """The client raises when the model says no product; for a case that expects
    exactly that, the refusal IS the answer."""
    provider = AsyncMock()
    provider.complete = AsyncMock(return_value=_identification(product_found=False, product_name=""))

    with patch("app.services.vision_client.resolve_vision_llm", return_value=provider):
        result = await tasks.identify_product(item={"input": {"hint_text": "quero", "countries": ["BRA"]}})

    assert result == {"product_found": False, "product_name": None, "category": None, "brand": None}


async def test_catalog_matching_builds_candidates_from_the_case() -> None:
    """The matcher maps the model's entity_key back to a candidate, so the fake
    echoes the first key the prompt showed it — the same thing a model does."""
    import re

    class _EchoFirstCandidate:
        def __init__(self) -> None:
            self.user: str | None = None

        async def complete(self, *, system=None, user, image=None, response_model=None):
            from app.services.catalog_matcher import CatalogMatchDecision

            self.user = user
            key = re.search(r"entity_key: (\S+)", user).group(1)
            return CatalogMatchDecision(entity_key=key, reasoning="same product")

    provider = _EchoFirstCandidate()

    with patch("evaluation.tasks._catalog_provider", return_value=provider):
        result = await tasks.match_catalog(
            item={
                "input": {
                    "text": "freidora de aire",
                    "candidates": [{"title": "air fryer", "category": "Kitchen Appliances"}],
                }
            }
        )

    assert result == {"matched_title": "air fryer"}
    assert "air fryer" in provider.user
    assert "freidora de aire" in provider.user


async def test_catalog_matching_reports_no_match_as_none() -> None:
    from app.services.catalog_matcher import CatalogMatchDecision

    provider = AsyncMock()
    provider.complete = AsyncMock(return_value=CatalogMatchDecision(entity_key="", reasoning="none of them"))

    with patch("evaluation.tasks._catalog_provider", return_value=provider):
        result = await tasks.match_catalog(
            item={"input": {"text": "manzana", "candidates": [{"title": "Apple iPhone", "category": "Smartphones"}]}}
        )

    assert result == {"matched_title": None}


async def test_reference_markets_asks_for_the_supported_basket() -> None:
    provider = AsyncMock()
    provider.complete = AsyncMock(
        return_value=ReferenceCandidatesResult(
            candidates=[
                MarketTerms(country="USA", primary_name="air fryer", search_terms=["air fryer"], confidence=0.9),
                MarketTerms(country="BRA", primary_name="fritadeira", search_terms=["fritadeira"], confidence=0.9),
            ]
        )
    )

    with patch("app.services.reference_market_client.resolve_reference_market_llm", return_value=provider):
        result = await tasks.reference_markets(
            item={"input": {"product_name": "air fryer", "brand": None, "category": "Kitchen Appliances"}}
        )

    assert result == {"markets": ["USA", "BRA"]}
    assert "USA" in provider.complete.await_args.kwargs["user"]


def test_every_dataset_has_a_task() -> None:
    from evaluation import cases

    assert set(tasks.TASKS) == set(cases.DATASETS)


def test_input_is_read_from_either_item_shape() -> None:
    class _Item:
        input = {"hint_text": "x"}

    assert tasks.input_of({"input": {"hint_text": "x"}}) == {"hint_text": "x"}
    assert tasks.input_of(_Item()) == {"hint_text": "x"}
