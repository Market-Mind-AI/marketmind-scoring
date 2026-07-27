"""Fase 2: product catalog resolution pipeline (docs/marketmind-implementation-plan.md
section 6). Cheapest-to-most-expensive match, in order:

1. Exact normalized alias (product_aliases) — O(1) index lookup.
2. Fuzzy trigram (pg_trgm) over existing aliases.
3. Embedding/semantic similarity over product_catalog.embedding.
4. Nothing matched — create a new entity locally (entity_key is always a
   freshly generated UUID, never blocked by an external dependency).

A successful trigram or embedding match writes a new exact alias pointing at
the matched entity, so the same text resolves via the O(1) path next time.
The embedding step is best-effort and never blocks resolution: if the
provider call fails (or OPENAI_API_KEY isn't set), resolution falls straight
through to entity creation with a null embedding — see embedding_client.py.
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger
from app.db.models.product_catalog import ProductCatalog
from app.repositories.product_repository import ProductRepository
from app.schemas.catalog import CatalogResolveResponse
from app.services.embedding_client import EmbeddingClient, EmbeddingError
from app.utils.text import normalize_alias


def _union_terms(*lists: list[str] | None) -> list[str]:
    """Case-insensitive union preserving first-seen order (existing terms first).
    Curated keywords are ACCUMULATED, never replaced — a re-identify only appends
    genuinely new terms, keeping the seed order (and thus the demand cache) stable."""
    seen: set[str] = set()
    out: list[str] = []
    for lst in lists:
        for term in lst or []:
            if not term:
                continue
            key = str(term).strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(term)
    return out


def _merge_localized_markets(
    existing: dict[str, dict[str, Any]] | None, incoming: dict[str, dict[str, Any]] | None
) -> dict[str, dict[str, Any]]:
    """Merge per-country search vocabularies: union search_terms/excluded_terms
    (existing first + new appended), so nothing curated is ever lost and only new
    terms are added to the base."""
    merged: dict[str, dict[str, Any]] = {k: dict(v) for k, v in (existing or {}).items()}
    for iso3, new in (incoming or {}).items():
        cur = merged.get(iso3)
        if not cur:
            merged[iso3] = new
            continue
        primary = [new.get("primary_name")] if new.get("primary_name") else None
        merged[iso3] = {
            "primary_name": cur.get("primary_name") or new.get("primary_name"),
            "formal_name": cur.get("formal_name") or new.get("formal_name"),
            "search_terms": _union_terms(cur.get("search_terms"), new.get("search_terms"), primary),
            "excluded_terms": _union_terms(cur.get("excluded_terms"), new.get("excluded_terms")),
            "confidence": max(float(cur.get("confidence") or 0.0), float(new.get("confidence") or 0.0)),
        }
    return merged


def _merge_reference_candidates(
    existing: list[dict[str, Any]] | None, incoming: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """Merge reference-market candidates by country (union of terms), same
    accumulate-never-replace rule as localized_markets."""
    by_country: dict[str | None, dict[str, Any]] = {c.get("country"): dict(c) for c in (existing or [])}
    for new in incoming or []:
        cur = by_country.get(new.get("country"))
        if not cur:
            by_country[new.get("country")] = new
            continue
        primary = [new.get("primary_name")] if new.get("primary_name") else None
        cur["search_terms"] = _union_terms(cur.get("search_terms"), new.get("search_terms"), primary)
        cur["excluded_terms"] = _union_terms(cur.get("excluded_terms"), new.get("excluded_terms"))
        cur["primary_name"] = cur.get("primary_name") or new.get("primary_name")
    return list(by_country.values())


class CatalogService:
    def __init__(self, session: AsyncSession, embedding_client: EmbeddingClient | None = None) -> None:
        self._session = session
        self._repo = ProductRepository(session)
        self._embedding_client = embedding_client or EmbeddingClient()

    async def resolve(
        self,
        text: str,
        *,
        source: str,
        category: str | None = None,
        localized_markets: dict[str, dict[str, Any]] | None = None,
        reference_candidates: list[dict[str, Any]] | None = None,
        target_age_min: int | None = None,
        target_age_max: int | None = None,
    ) -> CatalogResolveResponse:
        alias_normalized = normalize_alias(text)

        existing = await self._repo.get_entity_by_alias_exact(alias_normalized)
        if existing is not None:
            existing_alias, entity = existing
            await self._repo.bump_alias_hit(existing_alias)
            # Re-analysis refreshes the entity with any fields the caller brought
            # (identify-image / analyze pass-through), so a product's markets and
            # reference candidates stay current instead of being frozen at creation.
            self._refresh_entity_fields(
                entity,
                category=category,
                localized_markets=localized_markets,
                reference_candidates=reference_candidates,
                target_age_min=target_age_min,
                target_age_max=target_age_max,
            )
            await self._session.commit()
            return CatalogResolveResponse(
                entity_key=entity.entity_key, canonical_title=entity.canonical_title, method="alias"
            )

        trigram = await self._repo.trigram_search(alias_normalized)
        if trigram is not None:
            await self._repo.create_alias(
                alias_normalized=alias_normalized, entity_key=trigram.product.entity_key, source=source
            )
            self._refresh_entity_fields(
                trigram.product,
                category=category,
                localized_markets=localized_markets,
                reference_candidates=reference_candidates,
                target_age_min=target_age_min,
                target_age_max=target_age_max,
            )
            await self._session.commit()
            return CatalogResolveResponse(
                entity_key=trigram.product.entity_key,
                canonical_title=trigram.product.canonical_title,
                method="trigram",
                match_score=trigram.similarity,
            )

        embedding = await self._try_embed(text)
        if embedding is not None:
            semantic = await self._repo.semantic_search(embedding)
            if semantic is not None:
                await self._repo.create_alias(
                    alias_normalized=alias_normalized, entity_key=semantic.product.entity_key, source=source
                )
                self._refresh_entity_fields(
                    semantic.product,
                    category=category,
                    localized_markets=localized_markets,
                    reference_candidates=reference_candidates,
                    target_age_min=target_age_min,
                    target_age_max=target_age_max,
                )
                await self._session.commit()
                return CatalogResolveResponse(
                    entity_key=semantic.product.entity_key,
                    canonical_title=semantic.product.canonical_title,
                    method="embedding",
                    match_score=1 - semantic.distance,  # pgvector cosine_distance = 1 - cosine_similarity
                )

        entity = await self._repo.create_entity(
            canonical_title=text,
            category=category,
            localized_markets=localized_markets,
            reference_candidates=reference_candidates,
            target_age_min=target_age_min,
            target_age_max=target_age_max,
            embedding=embedding,
        )
        await self._repo.create_alias(alias_normalized=alias_normalized, entity_key=entity.entity_key, source=source)
        await self._session.commit()
        return CatalogResolveResponse(
            entity_key=entity.entity_key, canonical_title=entity.canonical_title, method="created"
        )

    @staticmethod
    def _refresh_entity_fields(
        entity: ProductCatalog,
        *,
        category: str | None,
        localized_markets: dict[str, dict[str, Any]] | None,
        reference_candidates: list[dict[str, Any]] | None,
        target_age_min: int | None,
        target_age_max: int | None,
    ) -> None:
        """Update an already-resolved entity with the fields a re-analysis
        brought, so its curated data stays current. Only NON-None values are
        applied — a text-only resolve (no markets) never wipes existing data.
        The entity is session-attached, so the surrounding commit persists it."""
        if category is not None:
            entity.category = category
        # Merge, never overwrite: curated keywords accumulate in the base; a
        # re-identify only adds genuinely new terms (keeps the demand cache stable).
        if localized_markets is not None:
            entity.localized_markets = _merge_localized_markets(entity.localized_markets, localized_markets)
        if reference_candidates is not None:
            entity.reference_candidates = _merge_reference_candidates(entity.reference_candidates, reference_candidates)
        if target_age_min is not None:
            entity.target_age_min = target_age_min
        if target_age_max is not None:
            entity.target_age_max = target_age_max

    async def backfill_missing_embeddings(self, *, limit: int = 50) -> int:
        """Computes embeddings for entities created while the provider was
        unavailable (or before it was configured). Safe to run repeatedly —
        each call handles up to `limit` entities. Returns how many were
        updated."""
        entities = await self._repo.list_entities_missing_embedding(limit=limit)
        updated = 0
        for entity in entities:
            embedding = await self._try_embed(entity.canonical_title)
            if embedding is None:
                continue
            await self._repo.set_embedding(entity, embedding)
            updated += 1
        await self._session.commit()
        return updated

    async def _try_embed(self, text: str) -> list[float] | None:
        try:
            return await self._embedding_client.embed(text)
        except EmbeddingError as exc:
            logger.warning(f"Embedding step unavailable, continuing without it: {exc}")
            return None
