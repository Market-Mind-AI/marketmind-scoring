import uuid
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.db.models.product_alias import ProductAlias
from app.db.models.product_catalog import ProductCatalog
from app.utils.time import utc_now


@dataclass
class TrigramMatch:
    product: ProductCatalog
    similarity: float


@dataclass
class SemanticMatch:
    product: ProductCatalog
    distance: float


class ProductRepository:
    """Backs the resolution pipeline in app/services/catalog_service.py:
    exact alias -> trigram fuzzy -> embedding semantic -> create new entity,
    plus the best-effort google_mid enrichment and merge jobs."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_alias_exact(self, alias_normalized: str) -> ProductAlias | None:
        stmt = select(ProductAlias).where(ProductAlias.alias_normalized == alias_normalized)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_entity_by_alias_exact(self, alias_normalized: str) -> tuple[ProductAlias, ProductCatalog] | None:
        stmt = (
            select(ProductAlias, ProductCatalog)
            .join(ProductCatalog, ProductCatalog.entity_key == ProductAlias.entity_key)
            .where(ProductAlias.alias_normalized == alias_normalized)
        )
        result = await self._session.execute(stmt)
        row = result.first()
        return None if row is None else (row[0], row[1])

    async def get_by_entity_key(self, entity_key: uuid.UUID) -> ProductCatalog | None:
        stmt = (
            select(ProductCatalog)
            .where(ProductCatalog.entity_key == entity_key)
            .options(selectinload(ProductCatalog.aliases))
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def bump_alias_hit(self, alias: ProductAlias) -> None:
        alias.hit_count += 1
        await self._session.flush()

    async def trigram_search(self, alias_normalized: str) -> TrigramMatch | None:
        similarity = func.similarity(ProductAlias.alias_normalized, alias_normalized)
        stmt = (
            select(ProductCatalog, similarity.label("similarity"))
            .join(ProductAlias, ProductAlias.entity_key == ProductCatalog.entity_key)
            .where(similarity >= settings.catalog_trigram_similarity_threshold)
            .where(ProductCatalog.merged_into.is_(None))
            .order_by(similarity.desc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        row = result.first()
        if row is None:
            return None
        product, sim = row
        return TrigramMatch(product=product, similarity=float(sim))

    async def semantic_search(self, embedding: list[float]) -> SemanticMatch | None:
        distance = ProductCatalog.embedding.cosine_distance(embedding)
        stmt = (
            select(ProductCatalog, distance.label("distance"))
            .where(ProductCatalog.embedding.is_not(None))
            .where(ProductCatalog.merged_into.is_(None))
            .where(distance <= settings.catalog_embedding_distance_threshold)
            .order_by(distance.asc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        row = result.first()
        if row is None:
            return None
        product, dist = row
        return SemanticMatch(product=product, distance=float(dist))

    async def create_entity(
        self,
        *,
        canonical_title: str,
        category: str | None = None,
        localized_terms: dict[str, str] | None = None,
        target_age_min: int | None = None,
        target_age_max: int | None = None,
        embedding: list[float] | None = None,
    ) -> ProductCatalog:
        entity = ProductCatalog(
            entity_key=uuid.uuid4(),
            canonical_title=canonical_title,
            category=category,
            localized_terms=localized_terms,
            target_age_min=target_age_min,
            target_age_max=target_age_max,
            embedding=embedding,
            created_at=utc_now(),
        )
        self._session.add(entity)
        await self._session.flush()
        return entity

    async def create_alias(self, *, alias_normalized: str, entity_key: uuid.UUID, source: str) -> ProductAlias:
        alias = ProductAlias(
            id=uuid.uuid4(),
            alias_normalized=alias_normalized,
            entity_key=entity_key,
            source=source,
            hit_count=1,
            first_seen_at=utc_now(),
        )
        self._session.add(alias)
        await self._session.flush()
        return alias

    async def set_embedding(self, entity: ProductCatalog, embedding: list[float]) -> None:
        entity.embedding = embedding
        await self._session.flush()

    async def set_google_mid(self, entity: ProductCatalog, *, google_mid: str, google_type: str | None) -> None:
        entity.google_mid = google_mid
        entity.google_type = google_type
        await self._session.flush()

    async def list_entities_missing_mid(self, *, limit: int = 50) -> list[ProductCatalog]:
        stmt = (
            select(ProductCatalog)
            .where(ProductCatalog.google_mid.is_(None))
            .where(ProductCatalog.merged_into.is_(None))
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_entities_missing_embedding(self, *, limit: int = 50) -> list[ProductCatalog]:
        stmt = (
            select(ProductCatalog)
            .where(ProductCatalog.embedding.is_(None))
            .where(ProductCatalog.merged_into.is_(None))
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def find_merge_candidates(self) -> list[tuple[ProductCatalog, ProductCatalog]]:
        """Entities sharing the same (enriched) google_mid that haven't been
        merged yet — (keep, duplicate) pairs, oldest entity in each group
        wins. Kept simple (no self-join cleverness) since the catalog is
        small in the MVP; see merge_service."""
        stmt = (
            select(ProductCatalog)
            .where(ProductCatalog.google_mid.is_not(None))
            .where(ProductCatalog.merged_into.is_(None))
            .order_by(ProductCatalog.google_mid, ProductCatalog.created_at)
        )
        result = await self._session.execute(stmt)

        by_mid: dict[str, list[ProductCatalog]] = {}
        for entity in result.scalars().all():
            mid = entity.google_mid
            if mid is None:
                continue
            by_mid.setdefault(mid, []).append(entity)

        pairs: list[tuple[ProductCatalog, ProductCatalog]] = []
        for group in by_mid.values():
            if len(group) < 2:
                continue
            keep, *duplicates = group  # query orders by created_at -> oldest first
            pairs.extend((keep, dup) for dup in duplicates)
        return pairs

    async def merge(self, *, keep: ProductCatalog, duplicate: ProductCatalog) -> None:
        duplicate.merged_into = keep.entity_key
        await self._session.execute(
            update(ProductAlias)
            .where(ProductAlias.entity_key == duplicate.entity_key)
            .values(entity_key=keep.entity_key)
        )
        await self._session.flush()
