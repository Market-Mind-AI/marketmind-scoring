"""Reconciliation: compares coverage_index against the 4 services' raw
observation tables to catch drift — a coverage row claiming collection
happened when the backing data isn't actually there. "Job de baixa
prioridade" per docs/marketmind-implementation-plan.md section 5; no
in-process scheduler is added (MVP has no Celery/APScheduler, see memory
marketmind_core_architecture "Capacidade"). Intended to be invoked
periodically by an external cron/systemd timer via POST /admin/reconciliation/run.

Scope for Fase 1:
- Only the 3 country-level sources (WorldBank, OECD, UNData) are checked.
  PyTrends coverage rows are product-specific (entity_key) and the raw
  `interest_over_time` table is keyed by `keyword`, which coverage_index
  never stores — resolving entity_key -> keyword needs the product catalog
  (Fase 2). Those rows are reported as ``unmappable``, not silently skipped.
- Only the "coverage claims collected but no raw row exists" direction is
  checked — the dangerous one, since it produces false confidence that data
  is available. The reverse (raw data exists with no coverage_index row) is
  a full-table scan per source; deferred to Fase 7 (evolution).
- The join key for WorldBank/OECD/UNData is "the same country code the Core
  would have sent when dispatching collection" (see
  collection_client.resolve_source_country_code), not a guess at each raw
  table's internal semantics — this keeps the check self-consistent with
  what Core actually asked for, and works even where the exact meaning of
  a raw `country` column varies by source.
"""

from dataclasses import dataclass, field

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger
from app.db.external import get_external_engine
from app.db.models.coverage_index import CoverageIndex
from app.domain.enums import DataSource
from app.repositories.country_repository import CountryRepository
from app.services.collection_client import resolve_source_country_code

_RECONCILABLE_SOURCES = (DataSource.WORLDBANK, DataSource.OECD, DataSource.UNDATA)

_EXISTENCE_QUERIES: dict[DataSource, str] = {
    DataSource.WORLDBANK: (
        "SELECT 1 FROM worldbank_observations WHERE country_iso2 = :country_code "
        "AND indicator_code = :indicator_code LIMIT 1"
    ),
    DataSource.OECD: (
        "SELECT 1 FROM oecd_observations WHERE country = :country_code " "AND indicator_code = :indicator_code LIMIT 1"
    ),
    DataSource.UNDATA: (
        "SELECT 1 FROM undata_observations WHERE country = :country_code "
        "AND indicator_code = :indicator_code LIMIT 1"
    ),
}


@dataclass
class ReconciliationDrift:
    coverage_index_id: str
    source: str
    entity_key: str | None
    country: str
    indicator_code: str
    period_key: str
    reason: str


@dataclass
class ReconciliationReport:
    checked: int = 0
    drift: list[ReconciliationDrift] = field(default_factory=list)
    unmappable: list[ReconciliationDrift] = field(default_factory=list)


class ReconciliationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._country_repo = CountryRepository(session)

    async def reconcile(self) -> ReconciliationReport:
        report = ReconciliationReport()
        stmt = select(CoverageIndex).where(CoverageIndex.source.in_([s.value for s in _RECONCILABLE_SOURCES]))
        result = await self._session.execute(stmt)
        rows = result.scalars().all()

        for row in rows:
            report.checked += 1
            source = DataSource(row.source)
            country = await self._country_repo.get_by_iso3(row.country)
            if country is None:
                report.unmappable.append(_drift(row, "no canonical country mapping for this ISO3 code"))
                continue

            try:
                country_code = resolve_source_country_code(source, country)
            except Exception:  # noqa: BLE001 — any resolution failure is "unmappable", not a crash
                report.unmappable.append(_drift(row, f"no {source.value} country code mapping"))
                continue

            exists = await self._raw_row_exists(source, country_code, row.indicator_code)
            if not exists:
                report.drift.append(_drift(row, "coverage_index row has no matching raw observation"))

        if report.drift:
            logger.warning(f"Reconciliation found {len(report.drift)} drifted coverage_index row(s)")
        return report

    async def _raw_row_exists(self, source: DataSource, country_code: str, indicator_code: str) -> bool:
        query = _EXISTENCE_QUERIES[source]
        engine = get_external_engine(source)
        async with engine.connect() as connection:
            params = {"country_code": country_code, "indicator_code": indicator_code}
            result = await connection.execute(text(query), params)
            return result.first() is not None


def _drift(row: CoverageIndex, reason: str) -> ReconciliationDrift:
    return ReconciliationDrift(
        coverage_index_id=str(row.id),
        source=row.source,
        entity_key=str(row.entity_key) if row.entity_key else None,
        country=row.country,
        indicator_code=row.indicator_code,
        period_key=row.period_key,
        reason=reason,
    )
