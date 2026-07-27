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


async def test_resolve_persists_localized_markets_on_new_entity(db_session: AsyncSession) -> None:
    service = CatalogService(db_session, embedding_client=_mock_embedding_client())
    text = f"Jamon Iberico {uuid.uuid4()}"
    localized_markets = {
        "ESP": {
            "primary_name": "jamón ibérico",
            "formal_name": "jamón de bellota",
            "search_terms": ["jamón ibérico", "jamón pata negra"],
            "excluded_terms": ["jamón serrano"],
            "confidence": 0.9,
        },
        "PRT": {
            "primary_name": "presunto pata negra",
            "formal_name": None,
            "search_terms": ["presunto pata negra", "presunto de porco preto"],
            "excluded_terms": [],
            "confidence": 0.8,
        },
    }

    result = await service.resolve(text, source="test", localized_markets=localized_markets)
    assert result.method == "created"

    from app.repositories.product_repository import ProductRepository

    entity = await ProductRepository(db_session).get_by_entity_key(result.entity_key)
    assert entity is not None
    assert entity.localized_markets == localized_markets


async def test_resolve_merges_curated_terms_on_reanalysis(db_session: AsyncSession) -> None:
    # Re-analysis of an existing product (exact alias hit) MERGES its curated
    # terms — never overwrites (2026-07-27): keywords accumulate in the base, and
    # a re-identify only ADDS genuinely new terms (keeping the demand cache
    # stable). A new country is added; an existing country's terms are unioned
    # (existing first + new appended).
    service = CatalogService(db_session, embedding_client=_mock_embedding_client())
    text = f"Presunto {uuid.uuid4()}"
    at_creation = {
        "PRT": {
            "primary_name": "presunto pata negra",
            "formal_name": None,
            "search_terms": ["presunto pata negra"],
            "excluded_terms": [],
            "confidence": 0.7,
        }
    }
    refreshed = {
        "PRT": {  # same country, a NEW term → unioned
            "primary_name": "presunto pata negra",
            "formal_name": None,
            "search_terms": ["presunto ibérico"],
            "excluded_terms": [],
            "confidence": 0.6,
        },
        "ESP": {  # new country → added
            "primary_name": "jamón pata negra",
            "formal_name": None,
            "search_terms": ["jamón pata negra"],
            "excluded_terms": [],
            "confidence": 0.6,
        },
    }
    candidates = [{"country": "ESP", "primary_name": "jamón", "search_terms": ["jamon"], "confidence": 0.9}]

    created = await service.resolve(text, source="test", localized_markets=at_creation)
    again = await service.resolve(text, source="test", localized_markets=refreshed, reference_candidates=candidates)
    assert again.entity_key == created.entity_key  # same entity (exact alias hit)
    assert again.method == "alias"

    from app.repositories.product_repository import ProductRepository

    entity = await ProductRepository(db_session).get_by_entity_key(again.entity_key)
    assert entity is not None
    lm = entity.localized_markets
    assert set(lm) == {"PRT", "ESP"}  # existing country kept, new country added
    # PRT terms unioned (existing first, new appended) — nothing lost
    assert lm["PRT"]["search_terms"] == ["presunto pata negra", "presunto ibérico"]
    assert lm["ESP"]["search_terms"] == ["jamón pata negra"]
    assert entity.reference_candidates == candidates  # newly persisted on re-analysis


async def test_resolve_does_not_wipe_curated_fields_when_none_provided(db_session: AsyncSession) -> None:
    # A text-only resolve (no markets) must NOT wipe existing curated data.
    service = CatalogService(db_session, embedding_client=_mock_embedding_client())
    text = f"Presunto {uuid.uuid4()}"
    at_creation = {
        "PRT": {
            "primary_name": "presunto pata negra",
            "formal_name": None,
            "search_terms": ["presunto pata negra"],
            "excluded_terms": [],
            "confidence": 0.7,
        }
    }
    created = await service.resolve(text, source="test", localized_markets=at_creation)
    await service.resolve(text, source="test")  # no localized_markets

    from app.repositories.product_repository import ProductRepository

    entity = await ProductRepository(db_session).get_by_entity_key(created.entity_key)
    assert entity is not None
    assert entity.localized_markets == at_creation  # preserved


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
