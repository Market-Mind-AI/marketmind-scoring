import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import MarketMindCoreError
from app.core.logging import logger
from app.db.models.job_task import JobTask
from app.domain.enums import DataSource, IngestionRunStatus, JobTaskStatus
from app.repositories.country_repository import CountryRepository
from app.repositories.job_repository import JobRepository
from app.schemas.coverage import CoverageGap, GapStatus, JobTaskResult
from app.services.collection_client import CollectionClient, CollectionRequest

# PyTrends indicators collected worldwide (no country), dispatched under the
# sentinel ISO3 "WLD" — see analysis_service._ensure_worldwide_demand_coverage
# and collection_client._build_pytrends_request.
_COUNTRYLESS_INDICATORS = frozenset({"interest_by_country"})
WORLD_SENTINEL_ISO3 = "WLD"


class CoverageService:
    """Fase 1: coverage detection + on-demand collection dispatch. Does not
    know what an indicator *means* (that's Fase 3/4) — it only knows whether
    a (source, entity_key, country, indicator_code, period_key) gap is
    already covered and, if not, how to ask the right sibling service to
    collect it and wait for the result. See docs/marketmind-implementation-plan.md
    section 5."""

    def __init__(self, session: AsyncSession, collection_client: CollectionClient | None = None) -> None:
        self._session = session
        self._job_repo = JobRepository(session)
        self._country_repo = CountryRepository(session)
        self._collection_client = collection_client or CollectionClient()

    async def _stale(self, gaps: list[CoverageGap]) -> dict[int, bool]:
        """Whether each gap still needs collecting, asked of the service that
        owns the data. Keyed by id() of the gap, so two gaps that look alike
        are still answered separately.

        This used to read coverage_index, a local record of what had been
        collected. That was a second copy of a fact the collection services
        already hold, and the two disagreed: a demand gap is only ever created
        because the pytrends service just said it lacks those terms, and the
        local index could still call it fresh and skip the collection.

        One request per source, for every pair at once.
        """
        por_fonte: dict[DataSource, list[CoverageGap]] = {}
        for gap in gaps:
            por_fonte.setdefault(gap.source, []).append(gap)

        stale: dict[int, bool] = {}
        for source, do_source in por_fonte.items():
            if source == DataSource.PYTRENDS:
                # The exception, by design. Demand is per product and per
                # keyword, so there is nothing to pre-store and no freshness to
                # ask about — and the caller only builds a pytrends gap after
                # asking that service which terms it is missing. Asking again
                # here would be the same question twice.
                stale.update({id(gap): True for gap in do_source})
                continue
            # Cada serviço indexa o país do jeito dele, e perguntar no código
            # errado não dá erro: devolve ZERO pares, que a regra abaixo lê como
            # "nunca verificado" e portanto vencido. O UN Data indexa por ISO2, e
            # perguntar por ISO3 fazia a coleta INTEIRA dele ser refeita em toda
            # passagem, para sempre — caro, silencioso, e exatamente o oposto do
            # que um índice de frescor existe para fazer.
            como_pedir = {gap.country: await self._codigo_para(source, gap.country) for gap in do_source}
            indicadores = sorted({gap.indicator_code for gap in do_source})
            respostas = await self._collection_client.freshness(
                source,
                countries=sorted({c for c in como_pedir.values() if c}),
                indicators=indicadores,
                stale_after_days=settings.coverage_stale_after_days,
            )
            for gap in do_source:
                # A pair the service did not report on was never checked there,
                # which is stale — never "fresh by omission".
                chave = (como_pedir.get(gap.country), gap.indicator_code)
                stale[id(gap)] = respostas.get(chave, True)
        return stale

    async def _codigo_para(self, source: DataSource, iso3: str) -> str | None:
        """O código com que ESTE serviço indexa o país.

        World Bank e OECD respondem por ISO3; o UN Data guarda ISO2. Não há
        convenção comum a seguir — o que existe é a tabela `countries`, que tem
        as duas grafias, então a tradução acontece aqui e não vira suposição
        espalhada pelo código.
        """
        if source != DataSource.UNDATA:
            return iso3
        pais = await self._country_repo.get_by_iso3(iso3)
        return pais.iso2 if pais is not None else None

    async def check_coverage(self, gaps: list[CoverageGap]) -> list[GapStatus]:
        """Pure read — never dispatches collection. Used by callers that want to
        know what is missing before deciding how to handle it."""
        stale = await self._stale(gaps)
        return [
            GapStatus(
                source=gap.source,
                entity_key=gap.entity_key,
                country=gap.country,
                indicator_code=gap.indicator_code,
                period_key=gap.period_key,
                covered=not stale[id(gap)],
                last_fetched_at=None,
            )
            for gap in gaps
        ]

    async def check_or_collect(
        self, gaps: list[CoverageGap], *, requested_by: str | None
    ) -> tuple[uuid.UUID, list[JobTaskResult]]:
        """Creates a Job, resolves every gap (skip if already fresh, attach
        if already in flight elsewhere, otherwise dispatch + poll to
        completion), and finalizes the job. Fully synchronous for Fase 1 —
        Fase 5 decides when this needs to move behind 202+SSE instead."""
        job = await self._job_repo.create_job(
            requested_by=requested_by,
            request_payload={"gaps": [gap.model_dump(mode="json") for gap in gaps]},
        )
        job_id = job.id
        await self._session.commit()

        # Committed once per gap (not once for the whole job): each task's
        # outcome must durably persist regardless of what happens to other
        # gaps in the same request — one gap's HTTP timeout must not roll
        # back another gap's already-successful collection. This also keeps
        # the race-dedup rollback in create_task (see job_repository) scoped
        # to only that gap's own uncommitted insert attempt.
        # One freshness question for the whole batch, before any dispatch. Asked
        # once here rather than once per gap: it is one request per source
        # either way, and asking inside the loop would make it one per gap.
        stale = await self._stale(gaps)

        results = []
        for gap in gaps:
            result = await self._process_gap(job_id, gap, stale=stale[id(gap)])
            await self._session.commit()
            results.append(result)

        has_failed_task = any(r.status == JobTaskStatus.FAILED for r in results)
        await self._job_repo.finalize_job(job, has_failed_task=has_failed_task)
        await self._session.commit()
        return job_id, results

    async def _process_gap(self, job_id: uuid.UUID, gap: CoverageGap, *, stale: bool) -> JobTaskResult:
        if not gap.force and not stale:
            task = await self._job_repo.mark_skipped(
                job_id=job_id,
                source=gap.source.value,
                entity_key=gap.entity_key,
                country=gap.country,
                indicator_code=gap.indicator_code,
                period_key=gap.period_key,
            )
            return job_task_to_result(task)

        active = await self._job_repo.find_active_task(
            source=gap.source.value,
            entity_key=gap.entity_key,
            country=gap.country,
            indicator_code=gap.indicator_code,
            period_key=gap.period_key,
        )
        if active is not None:
            logger.info(
                f"Gap already in flight, attaching to task {active.id}: "
                f"{gap.source.value}/{gap.country}/{gap.indicator_code}"
            )
            return job_task_to_result(active)

        claimed_task = await self._job_repo.create_task(
            job_id=job_id,
            source=gap.source.value,
            entity_key=gap.entity_key,
            country=gap.country,
            indicator_code=gap.indicator_code,
            period_key=gap.period_key,
        )
        if claimed_task is None:
            # Lost the race: a concurrent request committed an equivalent
            # task between our SELECT and our INSERT.
            attached = await self._job_repo.find_active_task(
                source=gap.source.value,
                entity_key=gap.entity_key,
                country=gap.country,
                indicator_code=gap.indicator_code,
                period_key=gap.period_key,
            )
            if attached is not None:
                return job_task_to_result(attached)
            # It must have finished in that same window — treat as covered.
            skipped = await self._job_repo.mark_skipped(
                job_id=job_id,
                source=gap.source.value,
                entity_key=gap.entity_key,
                country=gap.country,
                indicator_code=gap.indicator_code,
                period_key=gap.period_key,
            )
            return job_task_to_result(skipped)

        return await self._dispatch_and_poll(claimed_task, gap)

    async def _dispatch_and_poll(self, task: JobTask, gap: CoverageGap) -> JobTaskResult:
        # Country-less indicators (the worldwide PyTrends interest-by-country
        # breakdown) use the sentinel ISO3 "WLD" and carry no real country —
        # the collection request is built without a country code, so skip the
        # canonical-countries lookup instead of failing on "WLD not found".
        if gap.indicator_code in _COUNTRYLESS_INDICATORS:
            country = None
        else:
            country = await self._country_repo.get_by_iso3(gap.country)
            if country is None:
                await self._job_repo.mark_failed(
                    task, error_message=f"Unknown country {gap.country} (not in canonical countries table)"
                )
                return job_task_to_result(task)

        try:
            ingestion_run_id = await self._collection_client.dispatch(
                CollectionRequest(
                    source=gap.source,
                    indicator_code=gap.indicator_code,
                    country=country,
                    collection_params=gap.collection_params,
                )
            )
            await self._job_repo.mark_running(task, ingestion_run_id=ingestion_run_id)
            status = await self._collection_client.poll_until_done(gap.source, ingestion_run_id)
        except MarketMindCoreError as exc:
            await self._job_repo.mark_failed(task, error_message=str(exc))
            return job_task_to_result(task)

        if status != IngestionRunStatus.SUCCESS:
            await self._job_repo.mark_failed(
                task, error_message=f"{gap.source.value} ingestion run ended with status={status.value}"
            )
            return job_task_to_result(task)

        # Nothing written back about freshness. The service that collected it
        # stamped its own last_checked_at, and that is now the only record —
        # which is the point: one fact, one place.
        await self._job_repo.mark_done(task)
        return job_task_to_result(task)


def job_task_to_result(task: JobTask) -> JobTaskResult:
    return JobTaskResult(
        id=task.id,
        source=DataSource(task.source),
        entity_key=task.entity_key,
        country=task.country,
        indicator_code=task.indicator_code,
        period_key=task.period_key,
        status=JobTaskStatus(task.status),
        ingestion_run_id=task.ingestion_run_id,
        error_message=task.error_message,
    )
