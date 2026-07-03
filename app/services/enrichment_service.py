"""Best-effort google_mid enrichment (Fase 2, plan section 6, step 5) — never
blocks entity creation or resolution. Reuses the Fase 1 collection_client to
ask PyTrends for suggestions on an entity's canonical_title, then reads the
parsed suggestions back from PyTrends' own database (raw SQL via
app/db/external.py, same pattern as reconciliation_service.py — the Core
never writes to that database).

Picking the "best" candidate is a deterministic rule over what PyTrends
returns, never an LLM call: exact case-insensitive title match to
canonical_title first, else the first suggestion (Google already returns
them ranked by relevance)."""

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger
from app.db.external import get_external_engine
from app.db.models.product_catalog import ProductCatalog
from app.domain.enums import DataSource, IngestionRunStatus
from app.repositories.product_repository import ProductRepository
from app.services.collection_client import CollectionClient, CollectionDispatchError, CollectionRequest


@dataclass
class EnrichmentReport:
    checked: int = 0
    enriched: int = 0
    no_match: int = 0
    failed: int = 0


class EnrichmentService:
    def __init__(self, session: AsyncSession, collection_client: CollectionClient | None = None) -> None:
        self._session = session
        self._repo = ProductRepository(session)
        self._collection_client = collection_client or CollectionClient()

    async def enrich_missing_mids(self, *, limit: int = 20) -> EnrichmentReport:
        report = EnrichmentReport()
        entities = await self._repo.list_entities_missing_mid(limit=limit)
        for entity in entities:
            report.checked += 1
            try:
                enriched = await self._enrich_one(entity)
            except CollectionDispatchError as exc:
                logger.warning(f"google_mid enrichment failed for {entity.entity_key}: {exc}")
                report.failed += 1
                continue
            if enriched:
                report.enriched += 1
            else:
                report.no_match += 1
        await self._session.commit()
        return report

    async def _enrich_one(self, entity: ProductCatalog) -> bool:
        ingestion_run_id = await self._collection_client.dispatch(
            CollectionRequest(
                source=DataSource.PYTRENDS,
                indicator_code="suggestions",
                country=None,
                collection_params={"keyword": entity.canonical_title},
            )
        )
        status = await self._collection_client.poll_until_done(DataSource.PYTRENDS, ingestion_run_id)
        if status != IngestionRunStatus.SUCCESS:
            raise CollectionDispatchError(f"pytrends suggestions run ended with status={status.value}")

        candidate = await self._best_suggestion(ingestion_run_id, entity.canonical_title)
        if candidate is None:
            return False

        title, google_type, mid = candidate
        await self._repo.set_google_mid(entity, google_mid=mid, google_type=google_type)
        return True

    async def _best_suggestion(
        self, ingestion_run_id: uuid.UUID, canonical_title: str
    ) -> tuple[str, str | None, str] | None:
        engine = get_external_engine(DataSource.PYTRENDS)
        async with engine.connect() as connection:
            trend_request_id_result = await connection.execute(
                text("SELECT id FROM trend_requests WHERE ingestion_run_id = :run_id"),
                {"run_id": ingestion_run_id},
            )
            trend_request_row = trend_request_id_result.first()
            if trend_request_row is None:
                return None

            suggestions_result = await connection.execute(
                text(
                    "SELECT title, type, mid FROM suggestions "
                    "WHERE trend_request_id = :trend_request_id AND mid IS NOT NULL "
                    "ORDER BY created_at ASC"
                ),
                {"trend_request_id": trend_request_row[0]},
            )
            candidates = suggestions_result.all()

        if not candidates:
            return None

        normalized_title = canonical_title.strip().casefold()
        for title, google_type, mid in candidates:
            if title.strip().casefold() == normalized_title:
                return title, google_type, mid
        first_title, first_type, first_mid = candidates[0]
        return first_title, first_type, first_mid
