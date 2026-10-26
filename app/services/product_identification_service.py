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

from app.schemas.catalog import IdentifyImageResponse, ImageMediaType
from app.services.catalog_service import CatalogService
from app.services.vision_client import MarketTerms, VisionClient


def _market_to_dict(market: MarketTerms) -> dict[str, Any]:
    return {
        "primary_name": market.primary_name,
        "formal_name": market.formal_name,
        "search_terms": market.search_terms,
        "excluded_terms": market.excluded_terms,
        # Produtos que se confundem com este e são outro (ver
        # CatalogService._index_excluded_terms): a evidência negativa que o
        # veto semântico compara com os termos do produto.
        "antagonist_terms": market.antagonist_terms,
        "contrast_attributes": market.contrast_attributes,
        # O idioma em que ESTES termos foram escritos, dito por quem os
        # escreveu. Era uma coluna preenchida à mão em `countries`, um valor
        # por país fixado para sempre — Índia e Canadá como "en", e nenhuma
        # forma de acrescentar um país sem alguém cadastrar a linha primeiro.
        "search_language": market.search_language,
        "confidence": market.confidence,
    }


def _fallback_market(iso3: str, product_name: str, brand: str | None) -> dict[str, Any]:
    """Last-resort entry so a country is never left without search terms: the
    product's own name/brand as the term (low confidence, flagged for review).
    Used by AnalysisService's deferred enrichment."""
    base = f"{brand} {product_name}".strip() if brand else product_name
    terms = list(dict.fromkeys(t for t in (base, product_name, brand) if t))
    return {
        "primary_name": base or product_name,
        "formal_name": None,
        "search_terms": terms,
        "excluded_terms": [],
        "antagonist_terms": [],
        "contrast_attributes": [],
        "confidence": 0.3,
    }


def _has_terms(entry: dict[str, Any] | None) -> bool:
    return bool(entry and entry.get("search_terms"))


class ProductIdentificationService:
    def __init__(
        self,
        session: AsyncSession,
        vision_client: VisionClient | None = None,
    ) -> None:
        self._session = session
        self._vision_client = vision_client or VisionClient()
        self._catalog_service = CatalogService(session)

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

        # A marca nunca entra no texto que mede semelhança (era o que fundia
        # produtos da mesma marca) e, desde 24/09/2026, também não cria entidade:
        # o catálogo guarda um produto por identidade e a marca é um vínculo da
        # análise — ver catalog_service.resolve.
        resolved = await self._catalog_service.resolve(
            identification.product_name,
            source="image_identification" if image_base64 else "text_identification",
            brand=identification.brand,
            category=identification.category,
            localized_markets=localized_markets or None,
            reference_candidates=None,  # deferred to the /analyze background task
            target_age_min=identification.target_age_min,
            target_age_max=identification.target_age_max,
            defining_attributes=identification.defining_attributes,
            antagonist_products=identification.antagonist_products,
            model=identification.model,
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
