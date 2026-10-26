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

O CATÁLOGO VEM ANTES DO MODELO (2026-10-26). Cada identificação adivinhava o
produto do zero (~21 s), e o que ela adivinhava mudava de uma vez para outra:
a mesma impressora voltava com 20–55, 18–65 e 20–60 anos de público, e a nota
oscilava sem nenhum dado novo. Agora, nesta ordem:

  1. Léxico: o texto é exatamente um termo de busca que um único produto (e
     marca) do catálogo declara. Devolve o que está gravado, sem modelo.
  2. Candidatos: trigram e vetor trazem os produtos que o texto pode ser, e a
     MESMA chamada de identificação decide se é um deles. Se for, devolve o
     gravado; o modelo só diz a marca.
  3. Nada resolveu: a identificação completa de antes, e o catálogo resolve.
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger
from app.db.models.brand import Brand
from app.db.models.product_catalog import ProductCatalog
from app.repositories.product_repository import ProductRepository
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
        self._repo = ProductRepository(session)

    async def identify(
        self,
        *,
        image_base64: str | None,
        image_media_type: ImageMediaType | None,
        hint_text: str | None,
        countries: list[str],
    ) -> IdentifyImageResponse:
        texto = (hint_text or "").strip()
        if texto and not image_base64:
            donos = await self._repo.owners_of_search_term(texto)
            if len(donos) == 1:
                [(entity_key, brand_key)] = donos
                entity = await self._repo.get_by_entity_key(entity_key)
                if entity is not None:
                    logger.info(f"Identificação léxica: {texto!r} é termo de busca de {entity.canonical_title!r}")
                    marca = await self._repo.get_brand(brand_key)
                    return await self._do_catalogo(entity, marca, countries, method="lexical")

        candidatos = await self._catalog_service.candidatos_para_identificacao(texto) if texto else []
        identification = await self._vision_client.identify_product(
            image_base64=image_base64,
            image_media_type=image_media_type,
            hint_text=hint_text,
            # Nenhum país: a identificação devolve só a identidade. Escrever os
            # termos de busca de cada país era a maior parte da espera — medido
            # em 26/10/2026, 7–9 s sem eles contra 18–32 s com um a três
            # países —, e o /analyze os gera de novo em segundo plano de
            # qualquer jeito (AnalysisService._ensure_product_enrichment), agora
            # com os atributos e antagônicos que esta chamada grava.
            countries=[],
            candidates=[descricao for _, descricao in candidatos] or None,
        )
        if identification.catalog_match is not None:
            entity = candidatos[identification.catalog_match - 1][0]
            logger.info(f"Identificação pelo catálogo: {texto!r} é {entity.canonical_title!r}")
            marca = (
                await self._repo.get_or_create_brand(name=identification.brand, source="catalog_identification")
                if identification.brand and identification.brand.strip()
                else None
            )
            return await self._do_catalogo(entity, marca, countries, method="catalog")

        pai = (
            candidatos[identification.broader_catalog_match - 1][0]
            if identification.broader_catalog_match is not None
            and 1 <= identification.broader_catalog_match <= len(candidatos)
            else None
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
            estimated_price=identification.estimated_price,
            price_currency=identification.price_currency,
            julgados={entidade.entity_key for entidade, _ in candidatos},
            pai_julgado=pai,
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

    async def _do_catalogo(
        self, entity: ProductCatalog, marca: Brand | None, countries: list[str], *, method: str
    ) -> IdentifyImageResponse:
        """O produto como o catálogo o guarda: nada é adivinhado de novo. O
        vocabulário é o desta marca (ou do produto, sem marca), só dos países
        pedidos; o que faltar o /analyze completa, como sempre."""
        await self._session.commit()
        mercados = await self._repo.markets_for(entity, marca.brand_key if marca is not None else None)
        localized_markets = {c: mercados[c] for c in countries if _has_terms(mercados.get(c))}
        return IdentifyImageResponse(
            entity_key=entity.entity_key,
            canonical_title=entity.canonical_title,
            method=method,
            product_name=entity.canonical_title,
            brand=marca.name if marca is not None else None,
            category=entity.category,
            localized_markets=localized_markets or None,
            reference_candidates=None,
            target_age_min=entity.target_age_min,
            target_age_max=entity.target_age_max,
            estimated_price=float(entity.estimated_price) if entity.estimated_price is not None else None,
            price_currency=entity.price_currency,
            confidence=1.0,
        )
