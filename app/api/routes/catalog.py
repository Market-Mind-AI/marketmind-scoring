import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.repositories.product_repository import ProductRepository
from app.schemas.catalog import (
    CatalogEntityResponse,
    CatalogResolveRequest,
    CatalogResolveResponse,
    IdentifyImageRequest,
    IdentifyImageResponse,
    ProductAliasItem,
)
from app.services.catalog_service import CatalogService
from app.services.product_identification_service import ProductIdentificationService
from app.services.vision_client import ProductNotIdentifiedError, VisionError

router = APIRouter()


def get_catalog_service(session: AsyncSession = Depends(get_session)) -> CatalogService:
    return CatalogService(session)


def get_product_identification_service(
    session: AsyncSession = Depends(get_session),
) -> ProductIdentificationService:
    return ProductIdentificationService(session)


@router.post(
    "/catalog/resolve",
    response_model=CatalogResolveResponse,
    summary="Resolve product identity",
    description=(
        "Resolves free-text product identification (already extracted by an "
        "upstream LLM step, out of scope here) to a stable entity_key: exact "
        "alias exato -> trigram fuzzy (arbitrado pelos excluded_terms) -> LLM -> nova entidade. "
        "entity_key is always generated locally and never blocked by an "
        "external dependency."
    ),
)
async def resolve(
    request: CatalogResolveRequest, service: CatalogService = Depends(get_catalog_service)
) -> CatalogResolveResponse:
    return await service.resolve(
        request.text,
        source=request.source,
        brand=request.brand,
        category=request.category,
        target_age_min=request.target_age_min,
        target_age_max=request.target_age_max,
    )


@router.post(
    "/catalog/identify-image",
    response_model=IdentifyImageResponse,
    summary="Identify a product from a photo",
    description=(
        "Synchronous, lightweight — one vision call (Claude), then resolves "
        "the identified product through the same Fase 2 pipeline as "
        "POST /catalog/resolve. Deliberately separate from POST /analyze: "
        "the frontend shows this result and lets the user correct any field "
        "(product_name, brand, category, target_age_min/max, estimated_price) "
        "before submitting the full analysis. estimated_price is the one "
        "field in this service that is an LLM guess, not sourced from a real "
        "API — never scored, context only."
    ),
)
async def identify_image(
    request: IdentifyImageRequest,
    service: ProductIdentificationService = Depends(get_product_identification_service),
) -> IdentifyImageResponse:
    try:
        return await service.identify(
            image_base64=request.image_base64,
            image_media_type=request.image_media_type,
            hint_text=request.hint_text,
            countries=request.countries,
        )
    except ProductNotIdentifiedError as exc:
        # No concrete product in the image/text — ask the user to supply one
        # (422, not 502: it's a client-input problem, not a provider failure).
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except VisionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get(
    "/catalog/{entity_key}",
    response_model=CatalogEntityResponse,
    summary="Get catalog entity",
    description=(
        "Returns the entity and its aliases. merged_into is surfaced "
        "explicitly (not hidden) when this entity was merged into another — "
        "callers should follow it rather than treat the entity as current."
    ),
)
async def get_entity(entity_key: str, session: AsyncSession = Depends(get_session)) -> CatalogEntityResponse:
    try:
        parsed_key = uuid.UUID(entity_key)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="entity_key must be a UUID") from exc

    repo = ProductRepository(session)
    entity = await repo.get_by_entity_key(parsed_key)
    if entity is None:
        raise HTTPException(status_code=404, detail=f"Entity {entity_key} not found")

    marca = await repo.get_brand(entity.brand_key)
    return CatalogEntityResponse(
        entity_key=entity.entity_key,
        canonical_title=entity.canonical_title,
        google_mid=entity.google_mid,
        google_type=entity.google_type,
        category=entity.category,
        brand=marca.name if marca is not None else None,
        generic_entity_key=entity.generic_entity_key,
        brands=list(entity.brands or []),
        merged_into=entity.merged_into,
        aliases=[
            ProductAliasItem(alias_normalized=a.alias_normalized, source=a.source, hit_count=a.hit_count)
            for a in entity.aliases
        ],
    )
