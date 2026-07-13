import random
import uuid
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.catalog_service import CatalogService
from app.services.embedding_client import EmbeddingError


def _one_hot() -> list[float]:
    """Random dimension, not fixed — see the identical helper/comment in
    test_product_repository.py for why a fixed index isn't safe here."""
    vec = [0.0] * settings.openai_embedding_dimensions
    vec[random.randrange(settings.openai_embedding_dimensions)] = 1.0
    return vec


def _mock_embedding_client(vector: list[float] | None = None) -> AsyncMock:
    client = AsyncMock()
    if vector is None:
        client.embed.side_effect = EmbeddingError("no key configured")
    else:
        client.embed.return_value = vector
    return client


async def test_resolve_creates_new_entity_when_nothing_matches(db_session: AsyncSession) -> None:
    service = CatalogService(db_session, embedding_client=_mock_embedding_client())
    text = f"Brand New Product {uuid.uuid4()}"

    result = await service.resolve(text, source="test")
    assert result.method == "created"
    assert result.canonical_title == text


async def test_resolve_persists_localized_terms_on_new_entity(db_session: AsyncSession) -> None:
    service = CatalogService(db_session, embedding_client=_mock_embedding_client())
    text = f"Jamon Iberico {uuid.uuid4()}"
    localized = {
        "es": ["jamón ibérico", "jamón pata negra"],
        "pt": ["presunto de porco preto", "presunto pata negra"],
        "en": ["iberico ham"],
    }

    result = await service.resolve(text, source="test", localized_terms=localized)
    assert result.method == "created"

    from app.repositories.product_repository import ProductRepository

    entity = await ProductRepository(db_session).get_by_entity_key(result.entity_key)
    assert entity is not None
    assert entity.localized_terms == localized


async def test_resolve_accumulates_localized_terms_across_analyses(db_session: AsyncSession) -> None:
    # The acervo grows on every resolve (not only creation): a second resolve
    # of the same text merges genuinely-new variations and dedups the rest.
    service = CatalogService(db_session, embedding_client=_mock_embedding_client())
    text = f"Presunto {uuid.uuid4()}"

    created = await service.resolve(text, source="test", localized_terms={"pt": ["presunto pata negra"]})
    again = await service.resolve(
        text,
        source="test",
        localized_terms={"pt": ["presunto pata negra", "presunto de porco preto"], "es": ["jamón pata negra"]},
    )
    assert again.entity_key == created.entity_key  # same entity (exact alias hit)

    from app.repositories.product_repository import ProductRepository

    entity = await ProductRepository(db_session).get_by_entity_key(again.entity_key)
    assert entity is not None
    assert entity.localized_terms == {
        "pt": ["presunto pata negra", "presunto de porco preto"],  # deduped + appended
        "es": ["jamón pata negra"],  # new language added
    }


async def test_resolve_second_call_hits_exact_alias(db_session: AsyncSession) -> None:
    service = CatalogService(db_session, embedding_client=_mock_embedding_client())
    text = f"Repeat Product {uuid.uuid4()}"

    first = await service.resolve(text, source="test")
    second = await service.resolve(text, source="test")
    assert second.method == "alias"
    assert second.entity_key == first.entity_key


async def test_resolve_trigram_typo_reuses_entity(db_session: AsyncSession) -> None:
    service = CatalogService(db_session, embedding_client=_mock_embedding_client())
    unique = uuid.uuid4().hex[:8]
    original = f"Portable Blender {unique}"
    typo = f"Portable Blendr {unique}"

    first = await service.resolve(original, source="test")
    second = await service.resolve(typo, source="test")
    assert second.method == "trigram"
    assert second.entity_key == first.entity_key


async def test_resolve_embedding_match_reuses_entity(db_session: AsyncSession) -> None:
    vector = _one_hot()
    service = CatalogService(db_session, embedding_client=_mock_embedding_client(vector))
    first_text = f"Alpha Gadget {uuid.uuid4()}"
    first = await service.resolve(first_text, source="test")
    assert first.method == "created"

    # Completely different text, but the (mocked) embedding client returns
    # the exact same vector -> should match via the embedding step, not trigram.
    second_text = f"Something Totally Different {uuid.uuid4()}"
    second = await service.resolve(second_text, source="test")
    assert second.method == "embedding"
    assert second.entity_key == first.entity_key


async def test_resolve_falls_through_to_creation_when_embedding_unavailable(db_session: AsyncSession) -> None:
    service = CatalogService(db_session, embedding_client=_mock_embedding_client())  # raises EmbeddingError
    result = await service.resolve(f"No Embedding Available {uuid.uuid4()}", source="test")
    assert result.method == "created"


async def test_backfill_missing_embeddings(db_session: AsyncSession) -> None:
    from app.repositories.product_repository import ProductRepository

    repo = ProductRepository(db_session)
    entity = await repo.create_entity(canonical_title=f"No embedding yet {uuid.uuid4()}")
    await db_session.commit()

    # Large limit: other tests in this suite also leave behind entities with
    # a NULL embedding (any resolve() with the "raises EmbeddingError" mock),
    # and list_entities_missing_embedding has no ORDER BY — a small limit
    # could arbitrarily miss this test's own entity.
    vector = _one_hot()
    service = CatalogService(db_session, embedding_client=_mock_embedding_client(vector))
    updated = await service.backfill_missing_embeddings(limit=10_000)
    assert updated >= 1

    refreshed = await repo.get_by_entity_key(entity.entity_key)
    assert refreshed is not None
    assert refreshed.embedding is not None
