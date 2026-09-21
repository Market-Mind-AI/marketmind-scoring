import uuid
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.domain.enums import DataSource, JobTaskStatus
from app.schemas.coverage import GapStatus, JobTaskResult
from app.schemas.mos import MosResultResponse
from app.services.analysis_service import AnalysisService

_ENTITY_KEY = uuid.uuid4()
_REQUEST_ID = uuid.uuid4()


def _analysis_request(
    *, entity_key: uuid.UUID | None = _ENTITY_KEY, countries: list[str] | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        id=_REQUEST_ID,
        entity_key=entity_key,
        countries=countries or ["USA"],
        canonical_title="air fryer",
        status="pending",
        user_prompt=None,
    )


def _mos_result(country: str) -> MosResultResponse:
    return MosResultResponse(
        id=uuid.uuid4(),
        entity_key=_ENTITY_KEY,
        country=country,
        period_type="month",
        period_key="2026-07",
        mos=Decimal("70.0"),
        mos_before_cap=Decimal("70.0"),
        capped_by=None,
        topsis_score=Decimal("0.7"),
        library_topsis_score=None,
        library_mos=None,
        mos_interpretation="Good Opportunity",
        explanation="Market Opportunity Score - Good Opportunity (70/100): A solid market worth pursuing.",
        availability_status="available",
        excluded_reason=None,
        computed_at=datetime(2026, 7, 8),
    )


def _mos_excluded(country: str, motivo: str) -> MosResultResponse:
    """Como o compute_mos devolve um país sem dado: sem MOS, com o motivo
    (Cap. 5 §5.3 — nunca some em silêncio)."""
    linha = _mos_result(country)
    return linha.model_copy(
        update={
            "mos": None,
            "mos_before_cap": None,
            "topsis_score": None,
            "availability_status": "unavailable",
            "excluded_reason": motivo,
            "mos_interpretation": None,
            "explanation": None,
        }
    )


def _patched_service(
    *, all_covered: bool, missing_terms: list[str] | None = None, related_exists: bool = True
) -> tuple[AsyncMock, dict[str, AsyncMock]]:
    """Patches every collaborator AnalysisService constructs internally and
    returns (session, mocks) so tests can assert on call args."""
    mocks: dict[str, AsyncMock] = {}

    def _install(name: str, target: str) -> AsyncMock:
        patcher = patch(target)
        cls = patcher.start()
        mocks[f"_patcher_{name}"] = patcher  # keep alive for stop() in caller
        instance = cls.return_value
        mocks[name] = instance
        return instance

    analysis_repo = _install("analysis_repo", "app.services.analysis_service.AnalysisRepository")
    product_repo = _install("product_repo", "app.services.analysis_service.ProductRepository")
    country_repo = _install("country_repo", "app.services.analysis_service.CountryRepository")
    indicator_repo = _install("indicator_repo", "app.services.analysis_service.IndicatorRepository")
    mos_repo = _install("mos_repo", "app.services.analysis_service.MosRepository")
    coverage_service = _install("coverage_service", "app.services.analysis_service.CoverageService")
    indicator_engine = _install("indicator_engine", "app.services.analysis_service.IndicatorEngine")
    mos_engine = _install("mos_engine", "app.services.analysis_service.MosEngine")
    weight_calibration = _install("weight_calibration", "app.services.analysis_service.WeightCalibrationService")
    # Deferred enrichment clients (constructed in __init__). The default test
    # entity already has USA terms + candidates, so these are never CALLED
    # (missing=[]), but patch them so no real provider is constructed/hit.
    enrichment_client = _install("enrichment_client", "app.services.analysis_service.EnrichmentClient")
    enrichment_client.localized_markets = AsyncMock(return_value=[])
    reference_market_client = _install("reference_market_client", "app.services.analysis_service.ReferenceMarketClient")
    reference_market_client.candidates = AsyncMock(return_value=[])
    explanation_client = _install("explanation_client", "app.services.analysis_service.ExplanationClient")
    # build_evidence assembles the deterministic packet (persisted as breakdown);
    # narrate turns it into the best-effort narrative string.
    _packet = {"countries": [{"country": "USA"}]}
    explanation_client.build_evidence = AsyncMock(return_value=_packet)
    explanation_client.narrate = AsyncMock(return_value="MOS narrative (mock)")

    analysis_repo.get = AsyncMock(return_value=_analysis_request())
    analysis_repo.mark_running = AsyncMock()
    analysis_repo.mark_done = AsyncMock()
    analysis_repo.mark_failed = AsyncMock()

    # Demand is now collected from Google Ads keyword data (2026-07-15) with the
    # country's OWN approved terms in localized_markets[ISO3] — no worldwide
    # arbitration, no per-country-series guard, no pytrends_indicators calls here.
    product_repo.get_by_entity_key = AsyncMock(
        return_value=SimpleNamespace(
            canonical_title="air fryer",
            entity_key=_ENTITY_KEY,
            # Produto sem marca: a partição é a genérica e não há família acima
            # dele, então nenhuma medição de categoria é disparada (o volume da
            # categoria só existe para produto COM marca — ver indicator_engine).
            brand_key=None,
            generic_entity_key=None,
            localized_markets={"USA": {"primary_name": "air fryer", "search_terms": ["air fryer", "airfryer"]}},
            reference_candidates=[{"country": "BRA", "primary_name": "fritadeira", "search_terms": ["air fryer br"]}],
            # No cached champion: champion_period_key is None, so these tests
            # exercise the compute-and-persist path. A product whose
            # champion_period_key equals the period under test takes the cached
            # branch instead and does no candidate collection at all.
            champion_country=None,
            champion_mos=None,
            champion_period_key=None,
            champion_computed_at=None,
        )
    )
    product_repo.set_champion = AsyncMock()
    analysis_repo.set_explanation = AsyncMock()
    mos_repo.get_mos_result = AsyncMock(return_value=None)

    # Demand Volume ruler: volumes read via compute_trend_score. Patched so the
    # BRA candidate (terms ["air fryer br"]) is bigger (2000) than requested USA
    # (1000) — the pool is candidates + requested, so BRA wins the ceiling.
    def _trend_vol(terms: list[str], location: str, period_key: str, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(raw_value=2000.0 if "air fryer br" in terms else 1000.0)

    trend_patcher = patch(
        "app.services.analysis_service.pytrends_indicators.compute_trend_score",
        AsyncMock(side_effect=_trend_vol),
    )
    trend_patcher.start()
    mocks["_patcher_trend"] = trend_patcher
    # Related rows already present for the seed set → no forced keywords_for_keywords
    # re-collection (so the all_covered path still skips collection).
    related_patcher = patch(
        "app.services.analysis_service.pytrends_indicators.related_rows_exist",
        AsyncMock(return_value=related_exists),
    )
    related_patcher.start()
    mocks["_patcher_related"] = related_patcher
    # Term-incremental demand: only terms missing volume data drive a
    # search_volume collection (default: none missing → no collection).
    missing_patcher = patch(
        "app.services.analysis_service.pytrends_indicators.missing_volume_terms",
        AsyncMock(return_value=list(missing_terms or [])),
    )
    missing_patcher.start()
    mocks["_patcher_missing"] = missing_patcher
    country_repo.get_by_iso3 = AsyncMock(
        return_value=SimpleNamespace(search_language="en", pytrends_geo=None, iso2="US")
    )

    # Availability-aware coverage (2026-07-22): a demand gap is force-recollected
    # when its computed indicator is missing/unavailable. Mirror all_covered —
    # when everything is covered the demand indicators are available (so no
    # forced re-collection), otherwise they are empty (forces collection).
    demand_values = (
        {
            code: SimpleNamespace(normalized_score=Decimal("50"), availability_status="available")
            for code in ("trend_score", "growth_score", "demand_stability_score", "search_intent_score")
        }
        if all_covered
        else {}
    )
    indicator_repo.get_values_for = AsyncMock(return_value=demand_values)

    coverage_service.check_coverage = AsyncMock(
        side_effect=lambda gaps: [
            GapStatus(
                source=g.source,
                entity_key=g.entity_key,
                country=g.country,
                indicator_code=g.indicator_code,
                period_key=g.period_key,
                covered=all_covered,
            )  # fmt: skip
            for g in gaps
        ]
    )
    coverage_service.check_or_collect = AsyncMock(return_value=(uuid.uuid4(), []))

    indicator_engine.compute_all = AsyncMock(return_value=[])
    mos_engine.find_incomplete_countries = AsyncMock(return_value={})  # completeness gate: nothing missing
    mos_engine.compute_mos = AsyncMock(return_value=[_mos_result("USA")])
    weight_calibration.calibrate = AsyncMock(return_value={})

    session = AsyncMock()
    return session, mocks


async def test_no_demand_collection_when_terms_already_cached() -> None:
    # The core resilience/efficiency rule ("só baixa se um termo novo aparece"):
    # when every term already has volume data (nothing missing) and the seed's
    # related rows exist, NO pytrends demand collection is dispatched for the
    # requested country — even though the analysis still runs.
    session, mocks = _patched_service(all_covered=True, missing_terms=[], related_exists=True)
    try:
        service = AnalysisService(session)
        await service.run(_REQUEST_ID)
    finally:
        for key, value in list(mocks.items()):
            if key.startswith("_patcher_"):
                value.stop()

    dispatched = [gap for call in mocks["coverage_service"].check_or_collect.await_args_list for gap in call.args[0]]
    by_code = {g.indicator_code: g for g in dispatched if g.source == DataSource.PYTRENDS and g.country == "USA"}
    # Only the intent collection is re-run (its indicator was unavailable); the
    # nothing new → no search_volume and no keywords_for_keywords collection.
    assert by_code == {}


# --- Nada parcial sai: insistir quando a causa é transitória (decisões D3/D5) ---


def _task(status: str, *, country: str = "USA", source: DataSource = DataSource.UNDATA) -> JobTaskResult:
    return JobTaskResult(
        id=uuid.uuid4(),
        source=source,
        entity_key=None,
        country=country,
        indicator_code="POPULATION_GROWTH",
        period_key="LATEST",
        status=JobTaskStatus(status),
        ingestion_run_id=None,
        error_message="undata ingestion run ended with status=failed" if status == "failed" else None,
    )


# --- Caminho do orchestrator: nada parcial sai por aqui também (D3/D8) ---
#
# O caminho antigo marcava `done` assim que o orchestrator mandasse qualquer
# narrativa, com result=[] e um breakdown de mentira. Como a cobrança está presa
# ao `done`, ligar a flag entregava análise vazia cobrada.


class _FakeStream:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def __aenter__(self) -> "_FakeStream":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def raise_for_status(self) -> None:
        return None

    async def aiter_lines(self):  # noqa: ANN202
        for line in self._lines:
            yield line


class _FakeClient:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def stream(self, *args: object, **kwargs: object) -> _FakeStream:
        return _FakeStream(self._lines)


async def _run_via_orchestrator(lines: list[str]) -> dict[str, AsyncMock]:
    session, mocks = _patched_service(all_covered=True)
    try:
        with patch("app.services.analysis_service.httpx.AsyncClient", lambda **kw: _FakeClient(lines)):
            await AnalysisService(session).run(_REQUEST_ID)
    finally:
        for key, value in list(mocks.items()):
            if key.startswith("_patcher_"):
                value.stop()
    return mocks


async def test_the_core_does_not_complete_what_the_agent_owns() -> None:
    """The scoring step emits its markets several steps before the champion is
    crowned and the evidence assembled. Completing on that event would bill a
    partial answer while the agent was still working on the rest of it."""
    mocks = await _run_via_orchestrator(
        ['data: {"score": {"mos_results": [{"country": "USA", "mos": 70.0}]}}']
    )

    mocks["analysis_repo"].mark_done.assert_not_awaited()
    mocks["analysis_repo"].mark_failed.assert_not_awaited()


async def test_an_error_on_the_stream_is_logged_not_recorded_twice() -> None:
    """The agent records its own analysis failures, with the reason. This side
    would only overwrite that with a vaguer one."""
    mocks = await _run_via_orchestrator(['data: {"error": "the sources do not hold the data"}'])

    mocks["analysis_repo"].mark_failed.assert_not_awaited()
    mocks["analysis_repo"].mark_done.assert_not_awaited()


async def test_an_unreachable_orchestrator_is_recorded_here() -> None:
    """The one failure the agent cannot report, because it never ran."""
    session, mocks = _patched_service(all_covered=True)
    try:
        with patch("app.services.analysis_service.httpx.AsyncClient", _refusing_client):
            await AnalysisService(session).run(_REQUEST_ID)
    finally:
        for key, value in list(mocks.items()):
            if key.startswith("_patcher_"):
                value.stop()

    mocks["analysis_repo"].mark_failed.assert_awaited_once()
    assert "could not be reached" in mocks["analysis_repo"].mark_failed.call_args.kwargs["error_message"]


def _refusing_client(**kwargs: object) -> "_FakeClient":
    raise ConnectionError("connection refused")




# --- Coverage: the tool the agent calls, exercised directly ---
#
# These used to run through the in-process loop that sequenced them. The loop is
# gone — the agent sequences now — so they call the step itself, which is what
# the /internal route exposes.


async def _ensure_coverage(**kwargs: object) -> dict[str, AsyncMock]:
    session, mocks = _patched_service(**kwargs)
    try:
        await AnalysisService(session).ensure_coverage(country_iso3="USA", entity_key=_ENTITY_KEY)
    finally:
        for key, value in list(mocks.items()):
            if key.startswith("_patcher_"):
                value.stop()
    return mocks


async def test_only_what_is_missing_is_collected() -> None:
    """Already-fresh data is never re-collected: that is what makes a repeated
    analysis cheap, and what keeps a rate-limited source out of trouble."""
    mocks = await _ensure_coverage(all_covered=False)

    mocks["coverage_service"].check_or_collect.assert_awaited()
    assert len(mocks["coverage_service"].check_or_collect.call_args.args[0]) > 0


async def test_nothing_is_collected_when_everything_is_fresh() -> None:
    mocks = await _ensure_coverage(all_covered=True)

    dispatched = [
        gap
        for call in mocks["coverage_service"].check_or_collect.await_args_list
        for gap in call.args[0]
    ]
    assert dispatched == []


async def test_only_genuinely_new_terms_are_downloaded() -> None:
    """Demand is collected term by term. A term that already has volume is
    never fetched again — every one of those calls is billed."""
    mocks = await _ensure_coverage(all_covered=False, missing_terms=["airfryer"], related_exists=False)

    dispatched = [
        gap
        for call in mocks["coverage_service"].check_or_collect.await_args_list
        for gap in call.args[0]
    ]
    terms = [t for gap in dispatched for t in (gap.collection_params or {}).get("keywords", [])]
    assert "airfryer" in terms
    assert "air fryer" not in terms, "a term that already has volume must not be re-collected"
