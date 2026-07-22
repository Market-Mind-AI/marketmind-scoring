"""Reads the DB-parametrized metric explanation content (metric_explanations +
metric_band_phrases) into an ExplanationCatalog. The DB rows are the editable
runtime source (seeded from app/services/mos/explanation_seed.py); loading here
is memoized on the instance, so create one repo per request and edits to the
tables take effect on the next request."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.metric_explanation import MetricBandPhrase, MetricExplanation
from app.services.mos.explanation_builder import ExplanationCatalog, MetricEntry


class MetricExplanationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._catalog: ExplanationCatalog | None = None

    async def catalog(self) -> ExplanationCatalog:
        if self._catalog is not None:
            return self._catalog

        phrases_by_code: dict[str, dict[str, str]] = {}
        for phrase in (await self._session.execute(select(MetricBandPhrase))).scalars():
            phrases_by_code.setdefault(phrase.metric_code, {})[phrase.band] = phrase.phrase

        metrics: dict[str, MetricEntry] = {}
        for row in (await self._session.execute(select(MetricExplanation))).scalars():
            metrics[row.metric_code] = MetricEntry(
                kind=row.metric_kind,
                label=row.label,
                meaning=row.meaning,
                higher_is_better=row.higher_is_better,
                band_phrases=phrases_by_code.get(row.metric_code, {}),
            )

        self._catalog = ExplanationCatalog(metrics=metrics)
        return self._catalog
