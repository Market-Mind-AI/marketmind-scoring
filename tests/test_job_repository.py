import uuid
from dataclasses import asdict, dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.job_repository import JobRepository


async def _new_job(session: AsyncSession) -> uuid.UUID:
    repo = JobRepository(session)
    job = await repo.create_job(requested_by="test", request_payload={})
    await session.commit()
    return job.id


@dataclass
class _GapKey:
    source: str
    entity_key: uuid.UUID | None
    country: str
    indicator_code: str
    period_key: str


async def test_create_task_dedup_via_unique_index(db_session: AsyncSession) -> None:
    repo = JobRepository(db_session)
    job_id = await _new_job(db_session)
    key = _GapKey(
        source="worldbank", entity_key=None, country="USA", indicator_code=f"gdp-{uuid.uuid4()}", period_key="2026"
    )

    first = await repo.create_task(job_id=job_id, **asdict(key))
    assert first is not None
    await db_session.commit()

    second = await repo.create_task(job_id=job_id, **asdict(key))
    assert second is None  # unique partial index rejected the duplicate in-flight task


async def test_find_active_task_sees_committed_pending_task(db_session: AsyncSession) -> None:
    repo = JobRepository(db_session)
    job_id = await _new_job(db_session)
    key = _GapKey(
        source="oecd", entity_key=None, country="ESP", indicator_code=f"cpi-{uuid.uuid4()}", period_key="2026-07"
    )

    created = await repo.create_task(job_id=job_id, **asdict(key))
    assert created is not None
    await db_session.commit()

    found = await repo.find_active_task(**asdict(key))
    assert found is not None
    assert found.id == created.id
    await db_session.commit()


async def test_find_active_task_ignores_done_tasks(db_session: AsyncSession) -> None:
    repo = JobRepository(db_session)
    job_id = await _new_job(db_session)
    key = _GapKey(
        source="undata",
        entity_key=None,
        country="BRA",
        indicator_code=f"population-{uuid.uuid4()}",
        period_key="2026",
    )

    task = await repo.create_task(job_id=job_id, **asdict(key))
    assert task is not None
    await repo.mark_done(task)
    await db_session.commit()

    found = await repo.find_active_task(**asdict(key))
    assert found is None

    # A DONE task doesn't block a new in-flight task for the same key either
    # (the partial unique index only covers pending/running).
    new_task = await repo.create_task(job_id=job_id, **asdict(key))
    assert new_task is not None
    await db_session.commit()
