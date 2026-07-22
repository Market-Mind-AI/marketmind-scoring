"""Fase 5 — POST /catalog/identify-image. The vision model brings everything it
can read from the photo (product name, brand, category, price/age, and — when
it manages — the per-country search terms). A separate TEXT LLM then ENRICHES
the result with whatever is missing, above all `localized_markets`: the local
vision model is unreliable at producing localized search vocabulary, and demand
collection cannot run without it. localized_markets therefore NEVER comes back
empty — vision → enrichment LLM → deterministic fallback, in that order.

Synchronous and lightweight, deliberately separate from POST /analyze (user
request, 2026-07-08): the frontend shows this result and lets the user correct
any field before submitting the full analysis.
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger
from app.repositories.country_repository import CountryRepository
from app.schemas.catalog import IdentifyImageResponse, ImageMediaType
from app.services.catalog_service import CatalogService
from app.services.enrichment_client import EnrichmentClient, EnrichmentError
from app.services.vision_client import MarketTerms, VisionClient


def _market_to_dict(market: MarketTerms) -> dict[str, Any]:
    return {
        "primary_name": market.primary_name,
        "formal_name": market.formal_name,
        "search_terms": market.search_terms,
        "excluded_terms": market.excluded_terms,
        "confidence": market.confidence,
    }


def _fallback_market(iso3: str, product_name: str, brand: str | None) -> dict[str, Any]:
    """Last-resort entry so a country is never left without search terms: the
    product's own name/brand as the term (low confidence, flagged for review)."""
    base = f"{brand} {product_name}".strip() if brand else product_name
    terms = list(dict.fromkeys(t for t in (base, product_name, brand) if t))
    return {
        "primary_name": base or product_name,
        "formal_name": None,
        "search_terms": terms,
        "excluded_terms": [],
        "confidence": 0.3,
    }


def _has_terms(entry: dict[str, Any] | None) -> bool:
    return bool(entry and entry.get("search_terms"))


class ProductIdentificationService:
    def __init__(
        self,
        session: AsyncSession,
        vision_client: VisionClient | None = None,
        enrichment_client: EnrichmentClient | None = None,
    ) -> None:
        self._session = session
        self._vision_client = vision_client or VisionClient()
        self._enrichment_client = enrichment_client or EnrichmentClient()
        self._catalog_service = CatalogService(session)
        self._country_repo = CountryRepository(session)

    async def identify(
        self, *, image_base64: str, image_media_type: ImageMediaType, hint_text: str | None, countries: list[str]
    ) -> IdentifyImageResponse:
        identification = await self._vision_client.identify_product(
            image_base64=image_base64,
            image_media_type=image_media_type,
            hint_text=hint_text,
            countries=countries,
        )

        # Vision brings whatever markets it managed; the ISO3-keyed dict persisted
        # on product_catalog.localized_markets is then completed for EVERY
        # requested country (enrichment LLM, then deterministic fallback).
        localized_markets: dict[str, dict[str, Any]] = {
            market.country: _market_to_dict(market)
            for market in identification.markets
            if _has_terms(_market_to_dict(market))
        }
        localized_markets = await self._complete_markets(
            localized_markets,
            countries=countries,
            product_name=identification.product_name,
            brand=identification.brand,
            category=identification.category,
        )

        resolve_text = (
            f"{identification.brand} {identification.product_name}".strip()
            if identification.brand
            else identification.product_name
        )
        resolved = await self._catalog_service.resolve(
            resolve_text,
            source="image_identification",
            category=identification.category,
            localized_markets=localized_markets or None,
            target_age_min=identification.target_age_min,
            target_age_max=identification.target_age_max,
        )

        return IdentifyImageResponse(
            entity_key=resolved.entity_key,
            canonical_title=resolved.canonical_title,
            method=resolved.method,
            product_name=identification.product_name,
            brand=identification.brand,
            category=identification.category,
            localized_markets=localized_markets or None,
            target_age_min=identification.target_age_min,
            target_age_max=identification.target_age_max,
            estimated_price=identification.estimated_price,
            price_currency=identification.price_currency,
            confidence=identification.confidence,
        )

    async def _complete_markets(
        self,
        localized_markets: dict[str, dict[str, Any]],
        *,
        countries: list[str],
        product_name: str,
        brand: str | None,
        category: str | None,
    ) -> dict[str, dict[str, Any]]:
        """Guarantee a usable localized_markets entry for every requested
        country: fill the ones vision missed via the enrichment LLM, then a
        deterministic fallback for anything still missing. Never raises."""
        missing = [c for c in countries if not _has_terms(localized_markets.get(c))]
        if not missing:
            return localized_markets

        try:
            country_langs = [(iso3, await self._search_language(iso3)) for iso3 in missing]
            enriched = await self._enrichment_client.localized_markets(
                product_name=product_name, brand=brand, category=category, countries=country_langs
            )
            for market in enriched:
                entry = _market_to_dict(market)
                if market.country in missing and _has_terms(entry):
                    localized_markets[market.country] = entry
        except EnrichmentError as exc:
            logger.warning(f"localized_markets enrichment failed, using deterministic fallback: {exc}")

        still_missing = [c for c in countries if not _has_terms(localized_markets.get(c))]
        for iso3 in still_missing:
            localized_markets[iso3] = _fallback_market(iso3, product_name, brand)
        if still_missing:
            logger.info(f"localized_markets deterministic fallback used for: {', '.join(still_missing)}")
        return localized_markets

    async def _search_language(self, iso3: str) -> str | None:
        country = await self._country_repo.get_by_iso3(iso3)
        return country.search_language if country is not None else None
