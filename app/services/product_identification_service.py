"""Fase 5 — POST /catalog/identify-image. The vision model brings everything it
can read from the photo (product name, brand, category, price/age, and — when it
manages — the per-country search terms). A separate TEXT LLM then ENRICHES the
result with what vision is unreliable at:

  - localized_markets: filled for every requested country (vision → enrichment →
    deterministic fallback), so it NEVER comes back empty.
  - reference_market: the single supported country with the highest online
    search demand for the product — its absolute volume becomes the Demand
    Volume ruler ceiling (Cap. 4 §4.2.1). Always determined here.

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
from app.services.reference_market_client import ReferenceMarketClient, ReferenceMarketError
from app.services.vision_client import MarketTerms, VisionClient


def _market_to_dict(market: MarketTerms) -> dict[str, Any]:
    return {
        "primary_name": market.primary_name,
        "formal_name": market.formal_name,
        "search_terms": market.search_terms,
        "excluded_terms": market.excluded_terms,
        # O idioma em que ESTES termos foram escritos, dito por quem os
        # escreveu. Era uma coluna preenchida à mão em `countries`, um valor
        # por país fixado para sempre — Índia e Canadá como "en", e nenhuma
        # forma de acrescentar um país sem alguém cadastrar a linha primeiro.
        "search_language": market.search_language,
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
        reference_market_client: ReferenceMarketClient | None = None,
    ) -> None:
        self._session = session
        self._vision_client = vision_client or VisionClient()
        self._enrichment_client = enrichment_client or EnrichmentClient()
        self._reference_market_client = reference_market_client or ReferenceMarketClient()
        self._catalog_service = CatalogService(session)
        self._country_repo = CountryRepository(session)

    async def identify(
        self,
        *,
        image_base64: str | None,
        image_media_type: ImageMediaType | None,
        hint_text: str | None,
        countries: list[str],
    ) -> IdentifyImageResponse:
        identification = await self._vision_client.identify_product(
            image_base64=image_base64,
            image_media_type=image_media_type,
            hint_text=hint_text,
            countries=countries,
        )

        # Vision-only identify (fast): keep ONLY the terms the vision model
        # already returned. The heavy per-country enrichment (localized_markets)
        # and the reference-market candidates are DEFERRED to the /analyze
        # background task (AnalysisService._ensure_product_enrichment) — they are
        # the slow gpt-5 calls, and since the user no longer curates terms here
        # (only confirms identity) there is no reason to block on them now.
        localized_markets: dict[str, dict[str, Any]] = {
            market.country: _market_to_dict(market)
            for market in identification.markets
            if _has_terms(_market_to_dict(market))
        }

        # A marca PARTICIONA a identidade (nunca é concatenada ao texto que mede
        # semelhança, que foi o que fundia produtos da mesma marca). O que volta
        # é a entidade da marca, com a família genérica ligada — ver
        # catalog_service.resolve.
        resolved = await self._catalog_service.resolve(
            identification.product_name,
            source="image_identification" if image_base64 else "text_identification",
            brand=identification.brand,
            category=identification.category,
            localized_markets=localized_markets or None,
            reference_candidates=None,  # deferred to the /analyze background task
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
            reference_candidates=None,  # deferred to the /analyze background task
            target_age_min=identification.target_age_min,
            target_age_max=identification.target_age_max,
            estimated_price=identification.estimated_price,
            price_currency=identification.price_currency,
            confidence=identification.confidence,
        )

    async def _enrich(
        self,
        localized_markets: dict[str, dict[str, Any]],
        *,
        countries: list[str],
        product_name: str,
        brand: str | None,
        category: str | None,
    ) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
        """One enrichment call does both: fill the localized markets vision
        missed AND propose the ranked reference-market candidates. Guarantees
        localized_markets covers every requested country (enrichment, then
        deterministic fallback). The candidates are best-effort (empty if the LLM
        errors — the analysis then falls back to another ruler); real volume
        picks the winner among them later."""
        supported = [c.iso3 for c in await self._country_repo.list_all()]
        supported_set = set(supported)
        missing = [c for c in countries if not _has_terms(localized_markets.get(c))]

        # (1) localized_markets for the countries vision missed — local TEXT LLM.
        try:
            # Sem dica de idioma — ver a nota em analysis_service.
            fill = [(iso3, None) for iso3 in missing]
            for market in await self._enrichment_client.localized_markets(
                product_name=product_name, brand=brand, category=category, countries=fill
            ):
                entry = _market_to_dict(market)
                if market.country in missing and _has_terms(entry):
                    localized_markets[market.country] = entry
        except EnrichmentError as exc:
            logger.warning(f"localized_markets enrichment failed, using deterministic fallback: {exc}")

        # (2) reference-market candidates — a REAL cloud LLM (real market knowledge).
        reference_candidates: list[dict[str, Any]] = []
        try:
            seen: set[str] = set()
            for cand in await self._reference_market_client.candidates(
                product_name=product_name, brand=brand, category=category, supported_countries=supported
            ):
                entry = _market_to_dict(cand)
                if cand.country in supported_set and cand.country not in seen and _has_terms(entry):
                    reference_candidates.append({"country": cand.country, **entry})
                    seen.add(cand.country)
        except ReferenceMarketError as exc:
            logger.warning(f"Reference-market candidates unavailable: {exc}")

        still_missing = [c for c in countries if not _has_terms(localized_markets.get(c))]
        for iso3 in still_missing:
            localized_markets[iso3] = _fallback_market(iso3, product_name, brand)
        if still_missing:
            logger.info(f"localized_markets deterministic fallback used for: {', '.join(still_missing)}")
        if not reference_candidates:
            logger.info("No reference-market candidates — analysis will fall back to another Demand Volume ruler")
        return localized_markets, reference_candidates

