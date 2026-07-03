"""Catalog merge job (Fase 2, plan section 6, step 7): background
reconciliation that merges entities discovered to share the same
(best-effort-enriched) google_mid. Runs after enrichment_service, on
whatever cadence the caller wants — no in-process scheduler, same MVP
posture as reconciliation_service (Fase 1)."""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.product_repository import ProductRepository


@dataclass
class MergeReport:
    merged_pairs: int = 0


class MergeService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = ProductRepository(session)

    async def merge_duplicates(self) -> MergeReport:
        pairs = await self._repo.find_merge_candidates()
        for keep, duplicate in pairs:
            await self._repo.merge(keep=keep, duplicate=duplicate)
        await self._session.commit()
        return MergeReport(merged_pairs=len(pairs))
