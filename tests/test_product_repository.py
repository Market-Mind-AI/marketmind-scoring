import random
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.repositories.product_repository import ProductRepository


def _one_hot() -> list[float]:
    """A vector at a random dimension — NOT a fixed index. Reused runs
    against a persistent test DB (not recreated between manual `pytest`
    invocations, unlike CI) would otherwise collide with leftover rows from
    earlier runs at the same fixed index and produce flaky assertion
    failures on tie-broken ORDER BY ... LIMIT 1 queries."""
    vec = [0.0] * settings.openai_embedding_dimensions
    vec[random.randrange(settings.openai_embedding_dimensions)] = 1.0
    return vec


async def test_exact_alias_roundtrip(db_session: AsyncSession) -> None:
    repo = ProductRepository(db_session)
    entity = await repo.create_entity(canonical_title=f"Air Fryer {uuid.uuid4()}")
    alias_normalized = f"air fryer {uuid.uuid4()}"
    await repo.create_alias(alias_normalized=alias_normalized, entity_key=entity.entity_key, source="test")
    await db_session.commit()

    found = await repo.get_entity_by_alias_exact(alias_normalized)
    assert found is not None
    alias, found_entity = found
    assert found_entity.entity_key == entity.entity_key
    assert alias.hit_count == 1

    await repo.bump_alias_hit(alias)
    await db_session.commit()
    assert alias.hit_count == 2


async def test_trigram_search_finds_close_match(db_session: AsyncSession) -> None:
    repo = ProductRepository(db_session)
    unique = uuid.uuid4().hex[:8]
    entity = await repo.create_entity(canonical_title=f"Robot Vacuum Cleaner {unique}")
    await repo.create_alias(
        alias_normalized=f"robot vacuum cleaner {unique}", entity_key=entity.entity_key, source="test"
    )
    await db_session.commit()

    match = await repo.trigram_search(f"robot vacum cleaner {unique}")  # typo: "vacum"
    assert match is not None
    assert match.product.entity_key == entity.entity_key
    assert 0 < match.similarity <= 1


async def test_trigram_search_no_match_below_threshold(db_session: AsyncSession) -> None:
    repo = ProductRepository(db_session)
    match = await repo.trigram_search(f"totally-unrelated-query-{uuid.uuid4()}")
    assert match is None


async def test_semantic_search_finds_close_embedding(db_session: AsyncSession) -> None:
    repo = ProductRepository(db_session)
    close_vector = _one_hot()
    close_entity = await repo.create_entity(canonical_title=f"Close {uuid.uuid4()}", embedding=close_vector)
    await repo.create_entity(canonical_title=f"Far {uuid.uuid4()}", embedding=_one_hot())
    await db_session.commit()

    match = await repo.semantic_search(close_vector)
    assert match is not None
    assert match.product.entity_key == close_entity.entity_key
    assert match.distance < 0.01


async def test_semantic_search_ignores_far_embeddings(db_session: AsyncSession) -> None:
    repo = ProductRepository(db_session)
    await repo.create_entity(canonical_title=f"Only far {uuid.uuid4()}", embedding=_one_hot())
    await db_session.commit()

    match = await repo.semantic_search(_one_hot())  # different random dimension -> orthogonal -> above threshold
    assert match is None


async def test_merge_repoints_aliases_and_marks_duplicate(db_session: AsyncSession) -> None:
    repo = ProductRepository(db_session)
    mid = f"mid-{uuid.uuid4()}"
    keep = await repo.create_entity(canonical_title="Canonical")
    duplicate = await repo.create_entity(canonical_title="Duplicate")
    await repo.set_google_mid(keep, google_mid=mid, google_type="product")
    await repo.set_google_mid(duplicate, google_mid=mid, google_type="product")
    dup_alias_text = f"dup-alias-{uuid.uuid4()}"
    dup_alias = await repo.create_alias(alias_normalized=dup_alias_text, entity_key=duplicate.entity_key, source="test")
    await db_session.commit()

    await repo.merge(keep=keep, duplicate=duplicate)
    await db_session.commit()

    assert duplicate.merged_into == keep.entity_key
    await db_session.refresh(dup_alias)
    assert dup_alias.entity_key == keep.entity_key


async def test_find_merge_candidates_groups_by_shared_mid(db_session: AsyncSession) -> None:
    repo = ProductRepository(db_session)
    mid = f"mid-{uuid.uuid4()}"
    first = await repo.create_entity(canonical_title="First")
    second = await repo.create_entity(canonical_title="Second")
    await repo.set_google_mid(first, google_mid=mid, google_type="product")
    await repo.set_google_mid(second, google_mid=mid, google_type="product")
    await db_session.commit()

    pairs = await repo.find_merge_candidates()
    target_keys = {first.entity_key, second.entity_key}
    matching = [(keep, dup) for keep, dup in pairs if {keep.entity_key, dup.entity_key} == target_keys]
    assert len(matching) == 1
    keep, dup = matching[0]
    assert keep.entity_key == first.entity_key  # created first -> kept
    assert dup.entity_key == second.entity_key
