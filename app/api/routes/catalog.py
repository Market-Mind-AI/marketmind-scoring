import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.repositories.product_repository import ProductRepository
from app.schemas.catalog import CatalogEntityResponse, CatalogResolveRequest, CatalogResolveResponse, ProductAliasItem
from app.services.catalog_service import CatalogService

router = APIRouter()


def get_catalog_service(session: AsyncSession = Depends(get_session)) -> CatalogService:
    return CatalogService(session)


@router.post(
    "/catalog/resolve",
    response_model=CatalogResolveResponse,
    summary="Resolve product identity",
    description=(
        "Resolves free-text product identification (already extracted by an "
        "upstream LLM step, out of scope here) to a stable entity_key: exact "
        "alias -> fuzzy trigram -> embedding similarity -> create new entity. "
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
        category=request.category,
        target_age_min=request.target_age_min,
        target_age_max=request.target_age_max,
    )


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

    return CatalogEntityResponse(
        entity_key=entity.entity_key,
        canonical_title=entity.canonical_title,
        google_mid=entity.google_mid,
        google_type=entity.google_type,
        category=entity.category,
        merged_into=entity.merged_into,
        aliases=[
            ProductAliasItem(alias_normalized=a.alias_normalized, source=a.source, hit_count=a.hit_count)
            for a in entity.aliases
        ],
    )
