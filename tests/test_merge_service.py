import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.product_repository import ProductRepository
from app.services.merge_service import MergeService


async def test_merge_duplicates_merges_entities_sharing_mid(db_session: AsyncSession) -> None:
    repo = ProductRepository(db_session)
    mid = f"mid-{uuid.uuid4()}"
    keep = await repo.create_entity(canonical_title="Kept Entity")
    duplicate = await repo.create_entity(canonical_title="Duplicate Entity")
    await repo.set_google_mid(keep, google_mid=mid, google_type="product")
    await repo.set_google_mid(duplicate, google_mid=mid, google_type="product")
    await db_session.commit()

    report = await MergeService(db_session).merge_duplicates()
    assert report.merged_pairs >= 1

    await db_session.refresh(duplicate)
    assert duplicate.merged_into == keep.entity_key
