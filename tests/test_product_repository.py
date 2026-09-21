import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.product_repository import ProductRepository


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

    resultado = await repo.trigram_search(f"robot vacum cleaner {unique}")  # typo: "vacum"
    assert resultado.match is not None
    assert resultado.match.product.entity_key == entity.entity_key
    assert 0 < resultado.match.similarity <= 1
    assert resultado.vetoed == []


async def test_trigram_search_no_match_below_threshold(db_session: AsyncSession) -> None:
    repo = ProductRepository(db_session)
    resultado = await repo.trigram_search(f"totally-unrelated-query-{uuid.uuid4()}")
    assert resultado.match is None
    assert resultado.vetoed == [], "nada bateu não é a mesma coisa que algo ter sido vetado"
