"""What an experiment actually runs: the same clients a request runs.

A task calls the production client, so an experiment measures the prompt as it
is used — including the resolver, the prompt store and the traced provider —
rather than a copy of it written for the test.
"""

import uuid
from collections.abc import Callable
from typing import Any

from app.services.catalog_matcher import CatalogMatcher
from app.services.llm.resolver import resolve_catalog_match_llm
from app.services.reference_market_client import ReferenceMarketClient
from app.services.vision_client import ProductNotIdentifiedError, VisionClient
from evaluation.cases import SUPPORTED_COUNTRIES


def input_of(item: Any) -> dict:
    """The case's input, whether it arrives as a Langfuse item or a plain dict."""
    if isinstance(item, dict):
        return item["input"]
    return item.input


def _catalog_provider():
    """Indirection so a test can supply a provider without a database."""
    return resolve_catalog_match_llm()


class _Candidate:
    """What CatalogMatcher reads off a catalog row. The rows themselves live in
    the database; a case names its candidates, so the experiment needs none."""

    def __init__(self, title: str, category: str | None) -> None:
        self.entity_key = uuid.uuid4()
        self.canonical_title = title
        self.category = category
        self.localized_markets = None


async def identify_product(*, item: Any, **kwargs: Any) -> dict:
    case = input_of(item)
    try:
        result = await VisionClient().identify_product(
            image_base64=None,
            image_media_type=None,
            hint_text=case["hint_text"],
            countries=case["countries"],
        )
    except ProductNotIdentifiedError:
        # The client refuses to return a product the model did not find. For a
        # case that expects exactly that, the refusal is the answer.
        return {"product_found": False, "product_name": None, "category": None, "brand": None}
    return {
        "product_found": True,
        "product_name": result.product_name,
        "category": result.category,
        "brand": result.brand,
    }


async def match_catalog(*, item: Any, **kwargs: Any) -> dict:
    case = input_of(item)
    candidates = [_Candidate(c["title"], c.get("category")) for c in case["candidates"]]
    chosen = await CatalogMatcher(provider=_catalog_provider()).pick(text=case["text"], candidates=candidates)
    return {"matched_title": chosen.canonical_title if chosen is not None else None}


async def reference_markets(*, item: Any, **kwargs: Any) -> dict:
    case = input_of(item)
    found = await ReferenceMarketClient().candidates(
        product_name=case["product_name"],
        brand=case.get("brand"),
        category=case.get("category"),
        supported_countries=list(SUPPORTED_COUNTRIES),
    )
    return {"markets": [market.country for market in found]}


TASKS: dict[str, Callable] = {
    "identify-product": identify_product,
    "catalog-match": match_catalog,
    "reference-markets": reference_markets,
}
