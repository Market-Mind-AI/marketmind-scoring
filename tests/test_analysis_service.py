import uuid
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.core.config import settings
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


async def test_run_happy_path_marks_done_with_result() -> None:
    session, mocks = _patched_service(all_covered=True)
    try:
        with patch("app.services.analysis_service.current_month_period_key", return_value="2026-07"):
            service = AnalysisService(session)
            await service.run(_REQUEST_ID)
    finally:
        for key, value in list(mocks.items()):
            if key.startswith("_patcher_"):
                value.stop()

    mocks["analysis_repo"].mark_running.assert_awaited_once()
    mocks["indicator_engine"].compute_all.assert_any_await(
        country_iso3="USA", entity_key=_ENTITY_KEY, period_key="2026-07", demand_ceiling=2000.0
    )
    # compute_all runs for the requested market (USA) AND the off-request
    # reference candidate (BRA), which is scored for the champion benchmark.
    assert mocks["indicator_engine"].compute_all.await_count == 2
    mocks["mos_engine"].compute_mos.assert_any_await(entity_key=_ENTITY_KEY, countries=["USA"], period_key="2026-07")
    mocks["coverage_service"].check_or_collect.assert_not_awaited()  # everything already covered
    mocks["analysis_repo"].mark_done.assert_awaited_once()
    result_arg = mocks["analysis_repo"].mark_done.call_args.kwargs["result"]
    assert result_arg[0]["country"] == "USA"
    assert result_arg[0]["mos_interpretation"] == "Good Opportunity"
    assert result_arg[0]["requested"] is True
    # USA is the only scored market here, so it is also the champion.
    assert result_arg[0]["is_champion"] is True
    # Deterministic packet built + persisted (breakdown); best-effort narrative
    # generated from it and persisted with the result.
    mocks["explanation_client"].build_evidence.assert_awaited_once()
    mocks["explanation_client"].narrate.assert_awaited_once()
    # The narrative is attached AFTER the answer, so mark_done carries none of it.
    # It arrives via set_explanation, which is what keeps a 249s LLM call from
    # sitting between a finished MOS and the caller.
    assert mocks["analysis_repo"].mark_done.call_args.kwargs["explanation"] is None
    mocks["analysis_repo"].set_explanation.assert_awaited_once()
    assert mocks["analysis_repo"].set_explanation.await_args.args[1] == "MOS narrative (mock)"
    assert mocks["analysis_repo"].mark_done.call_args.kwargs["breakdown"] == {
        "countries": [{"country": "USA", "requested": True, "is_champion": True}],
        "champion_country": "USA",
        "requested_countries": ["USA"],
    }
    # The Demand Volume ruler (reference market + volume) is handed to the packet builder.
    ruler = mocks["explanation_client"].build_evidence.call_args.kwargs["demand_ruler"]
    # Ceiling = largest among the CANDIDATES only (deterministic / query-independent);
    # the requested country USA is NOT in the pool. Candidate BRA -> 2000.
    assert ruler == {
        "mode": "reference",
        "country": "BRA",
        "volume": 2000.0,
        "candidates": [{"country": "BRA", "volume": 2000.0}],
    }


async def test_off_request_champion_is_surfaced_and_crowned() -> None:
    """When the best-MOS market is a reference candidate the user did NOT request,
    it is appended to the results, flagged requested=False + is_champion=True, and
    the requested market keeps requested=True (crowned only if it's the top)."""
    session, mocks = _patched_service(all_covered=True)

    # BRA (candidate) scores higher than requested USA → BRA is the champion.
    def _mos_by_country(*, entity_key: object, countries: list[str], period_key: str) -> list[MosResultResponse]:
        out = []
        for c in countries:
            r = _mos_result(c)
            if c == "BRA":
                r.mos = Decimal("85.0")
            out.append(r)
        return out

    mocks["mos_engine"].compute_mos = AsyncMock(side_effect=_mos_by_country)
    # build_evidence packet must carry both countries so run() can annotate them.
    mocks["explanation_client"].build_evidence = AsyncMock(
        return_value={"countries": [{"country": "USA"}, {"country": "BRA"}]}
    )
    try:
        with patch("app.services.analysis_service.current_month_period_key", return_value="2026-07"):
            service = AnalysisService(session)
            await service.run(_REQUEST_ID)
    finally:
        for key, value in list(mocks.items()):
            if key.startswith("_patcher_"):
                value.stop()

    result_arg = mocks["analysis_repo"].mark_done.call_args.kwargs["result"]
    by_country = {r["country"]: r for r in result_arg}
    assert set(by_country) == {"USA", "BRA"}  # off-request champion appended
    assert by_country["USA"]["requested"] is True
    assert by_country["USA"]["is_champion"] is False
    assert by_country["BRA"]["requested"] is False  # not requested — benchmark only
    assert by_country["BRA"]["is_champion"] is True  # crowned
    breakdown = mocks["analysis_repo"].mark_done.call_args.kwargs["breakdown"]
    assert breakdown["champion_country"] == "BRA"
    assert breakdown["requested_countries"] == ["USA"]
    bra = next(c for c in breakdown["countries"] if c["country"] == "BRA")
    assert bra["is_champion"] is True and bra["requested"] is False


async def test_cached_champion_skips_all_candidate_work() -> None:
    """A champion already computed for this period costs nothing to reuse.

    This is the property that makes a repeat analysis fast. Before it existed,
    three consecutive analyses of the same product and countries took 594s, 203s
    and 396s on the deployed environment — the requested countries collected
    nothing on the repeats, and every second went to re-collecting candidate
    demand from PyTrends to rediscover the same champion.

    So the assertion is about work NOT done: no candidate coverage, no candidate
    scoring, and no rewrite of the stored champion.
    """
    session, mocks = _patched_service(all_covered=True)
    entity = mocks["product_repo"].get_by_entity_key.return_value
    entity.champion_country = "BRA"
    entity.champion_mos = Decimal("85.0")
    entity.champion_period_key = "2026-07"  # the period under test

    cached_row = _mos_result("BRA")
    cached_row.mos = Decimal("85.0")
    mocks["mos_repo"].get_mos_result = AsyncMock(return_value=cached_row)

    mocks["explanation_client"].build_evidence = AsyncMock(
        return_value={"countries": [{"country": "USA"}, {"country": "BRA"}]}
    )
    try:
        with patch("app.services.analysis_service.current_month_period_key", return_value="2026-07"):
            service = AnalysisService(session)
            await service.run(_REQUEST_ID)
    finally:
        for key, value in list(mocks.items()):
            if key.startswith("_patcher_"):
                value.stop()

    # compute_mos is called once, for the requested countries only — never again
    # for candidates.
    called_country_sets = [c.kwargs["countries"] for c in mocks["mos_engine"].compute_mos.await_args_list]
    assert called_country_sets == [["USA"]], f"candidate scoring ran: {called_country_sets}"
    # The stored champion is left alone; nothing was recomputed to overwrite it.
    mocks["product_repo"].set_champion.assert_not_awaited()
    # And it still reaches the answer, read from its persisted MOS row.
    by_country = {r["country"]: r for r in mocks["analysis_repo"].mark_done.call_args.kwargs["result"]}
    assert by_country["BRA"]["is_champion"] is True
    assert by_country["BRA"]["requested"] is False


async def test_run_dispatches_collection_only_for_missing_gaps() -> None:
    session, mocks = _patched_service(all_covered=False)
    try:
        service = AnalysisService(session)
        await service.run(_REQUEST_ID)
    finally:
        for key, value in list(mocks.items()):
            if key.startswith("_patcher_"):
                value.stop()

    mocks["coverage_service"].check_or_collect.assert_awaited()
    dispatched_gaps = mocks["coverage_service"].check_or_collect.call_args.args[0]
    assert len(dispatched_gaps) > 0


async def test_demand_gaps_built_incrementally_from_new_terms() -> None:
    # Term-incremental demand: search_volume collects ONLY the terms missing
    # volume data (here "airfryer"), and keywords_for_keywords is collected
    # because the seed's related rows are absent (related_exists=False). ISO2
    # location + search_language, as before.
    session, mocks = _patched_service(all_covered=False, missing_terms=["airfryer"], related_exists=False)
    try:
        service = AnalysisService(session)
        await service.run(_REQUEST_ID)
    finally:
        for key, value in list(mocks.items()):
            if key.startswith("_patcher_"):
                value.stop()

    dispatched = [gap for call in mocks["coverage_service"].check_or_collect.await_args_list for gap in call.args[0]]
    usa_pytrends = {g.indicator_code: g for g in dispatched if g.source == DataSource.PYTRENDS and g.country == "USA"}
    assert set(usa_pytrends) == {"search_volume", "keywords_for_keywords"}

    sv = usa_pytrends["search_volume"]
    assert sv.entity_key == _ENTITY_KEY
    # only the missing term is downloaded, not the whole set
    assert sv.collection_params == {"location": "US", "language": "en", "keywords": ["airfryer"]}

    kfk = usa_pytrends["keywords_for_keywords"]
    assert kfk.collection_params == {"location": "US", "language": "en", "seed_keywords": ["air fryer", "airfryer"]}


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


async def test_pais_incompleto_ganha_segunda_coleta_antes_de_ser_excluido() -> None:
    """Até 2026-08-25 um país incompleto derrubava a ANÁLISE INTEIRA. Agora ele
    ganha uma segunda passada de coleta e recálculo — que pega falha transitória
    de coletor e é no-op quando o dado realmente não existe."""
    session, mocks = _patched_service(all_covered=True)
    mocks["mos_engine"].find_incomplete_countries = AsyncMock(
        side_effect=[{"USA": "Missing mandatory indicator(s): trend_score"}, {}]
    )
    chamadas_antes = mocks["indicator_engine"].compute_all.await_count
    try:
        service = AnalysisService(session)
        await service.run(_REQUEST_ID)
    finally:
        for key, value in list(mocks.items()):
            if key.startswith("_patcher_"):
                value.stop()

    assert mocks["indicator_engine"].compute_all.await_count > chamadas_antes, "recalculou o país incompleto"
    assert mocks["mos_engine"].find_incomplete_countries.await_count == 2, "reconferiu depois da 2ª passada"
    mocks["analysis_repo"].mark_done.assert_awaited_once()
    mocks["analysis_repo"].mark_failed.assert_not_awaited()


async def test_pais_sem_dado_e_excluido_e_os_outros_sao_entregues() -> None:
    """O que sobra incompleto é excluído COM MOTIVO e viaja no resultado — o
    relatório diz quem ficou de fora e por quê. Os demais são entregues."""
    session, mocks = _patched_service(all_covered=True)
    mocks["mos_engine"].find_incomplete_countries = AsyncMock(
        return_value={"BRA": "Missing mandatory indicator(s): growth_score"}
    )
    mocks["mos_engine"].compute_mos = AsyncMock(
        return_value=[_mos_result("USA"), _mos_excluded("BRA", "Missing mandatory indicator(s): growth_score")]
    )
    try:
        service = AnalysisService(session)
        await service.run(_REQUEST_ID)
    finally:
        for key, value in list(mocks.items()):
            if key.startswith("_patcher_"):
                value.stop()

    mocks["analysis_repo"].mark_done.assert_awaited_once()
    resultado = mocks["analysis_repo"].mark_done.call_args.kwargs["result"]
    por_pais = {linha["country"]: linha for linha in resultado}
    assert por_pais["USA"]["mos"] is not None
    assert por_pais["BRA"]["mos"] is None
    assert "growth_score" in por_pais["BRA"]["excluded_reason"]


async def test_falha_somente_quando_nenhum_pais_pode_ser_pontuado() -> None:
    """O único caso em que não há mais o que fazer."""
    session, mocks = _patched_service(all_covered=True)
    mocks["mos_engine"].find_incomplete_countries = AsyncMock(
        return_value={"USA": "Missing mandatory indicator(s): trend_score"}
    )
    mocks["mos_engine"].compute_mos = AsyncMock(
        return_value=[_mos_excluded("USA", "Missing mandatory indicator(s): trend_score")]
    )
    try:
        service = AnalysisService(session)
        await service.run(_REQUEST_ID)
    finally:
        for key, value in list(mocks.items()):
            if key.startswith("_patcher_"):
                value.stop()

    mocks["analysis_repo"].mark_failed.assert_awaited_once()
    mensagem = mocks["analysis_repo"].mark_failed.call_args.kwargs["error_message"]
    assert "no country could be scored" in mensagem
    assert "trend_score" in mensagem, "a mensagem tem de dizer o motivo, não só que falhou"
    mocks["analysis_repo"].mark_done.assert_not_awaited()


async def test_run_marks_failed_on_unexpected_exception() -> None:
    session, mocks = _patched_service(all_covered=True)
    mocks["indicator_engine"].compute_all = AsyncMock(side_effect=RuntimeError("boom"))
    try:
        service = AnalysisService(session)
        await service.run(_REQUEST_ID)
    finally:
        for key, value in list(mocks.items()):
            if key.startswith("_patcher_"):
                value.stop()

    mocks["analysis_repo"].mark_failed.assert_awaited_once()
    assert "boom" in mocks["analysis_repo"].mark_failed.call_args.kwargs["error_message"]
    mocks["analysis_repo"].mark_done.assert_not_awaited()


async def test_run_skips_mos_when_entity_key_is_none() -> None:
    session, mocks = _patched_service(all_covered=True)
    mocks["analysis_repo"].get = AsyncMock(return_value=_analysis_request(entity_key=None))
    try:
        service = AnalysisService(session)
        await service.run(_REQUEST_ID)
    finally:
        for key, value in list(mocks.items()):
            if key.startswith("_patcher_"):
                value.stop()

    mocks["mos_engine"].compute_mos.assert_not_awaited()
    mocks["analysis_repo"].mark_done.assert_awaited_once_with(
        mocks["analysis_repo"].get.return_value, result=[], explanation=None, breakdown=None
    )


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


async def test_falha_transitoria_de_fonte_faz_a_analise_insistir() -> None:
    """O incidente de 25/08: a API do UN Data respondeu 503 por 29 minutos e 9
    de 24 países saíram do ranking. A coleta FALHOU — a fonte não respondeu —, e
    o mesmo dado veio inteiro 36 segundos depois de ela voltar. Causa
    transitória não pode custar um país no resultado."""
    session, mocks = _patched_service(all_covered=False, missing_terms=[], related_exists=True)
    mocks["coverage_service"].check_or_collect = AsyncMock(return_value=(uuid.uuid4(), [_task("failed")]))
    # Incompleto na 1ª tentativa; completo na 2ª, depois que a fonte voltou.
    mocks["mos_engine"].find_incomplete_countries = AsyncMock(
        side_effect=[
            {"USA": "Missing mandatory indicator(s): population_growth_score"},
            {"USA": "Missing mandatory indicator(s): population_growth_score"},
            {},
        ]
    )
    dormiu = AsyncMock()
    try:
        with patch("app.services.analysis_service.asyncio.sleep", dormiu):
            service = AnalysisService(session)
            await service.run(_REQUEST_ID)
    finally:
        for key, value in list(mocks.items()):
            if key.startswith("_patcher_"):
                value.stop()

    dormiu.assert_awaited()  # esperou a fonte voltar em vez de excluir o país
    assert dormiu.await_args.args[0] == 30, "primeiro backoff curto: a maioria das falhas é blip"
    mocks["analysis_repo"].mark_done.assert_awaited_once()
    mocks["analysis_repo"].mark_failed.assert_not_awaited()


async def test_orcamento_esgotado_falha_em_vez_de_entregar_parcial() -> None:
    """A regra que substitui a de 9b84397: se sobrou país sem dado por falha de
    fonte e o orçamento acabou, a análise termina em `failed` com motivo. Nunca
    em `done` incompleta — o usuário paga por análise completa (D3)."""
    session, mocks = _patched_service(all_covered=False, missing_terms=[], related_exists=True)
    mocks["coverage_service"].check_or_collect = AsyncMock(return_value=(uuid.uuid4(), [_task("failed")]))
    mocks["mos_engine"].find_incomplete_countries = AsyncMock(
        return_value={"USA": "Missing mandatory indicator(s): population_growth_score"}
    )
    try:
        with patch.object(settings, "analysis_retry_budget_seconds", 0):
            service = AnalysisService(session)
            await service.run(_REQUEST_ID)
    finally:
        for key, value in list(mocks.items()):
            if key.startswith("_patcher_"):
                value.stop()

    mocks["analysis_repo"].mark_done.assert_not_awaited()
    mocks["analysis_repo"].mark_failed.assert_awaited_once()
    motivo = mocks["analysis_repo"].mark_failed.call_args.kwargs["error_message"]
    assert "USA" in motivo and "sem dado por falha de fonte" in motivo


async def test_fonte_sem_o_dado_nao_vira_espera_infinita() -> None:
    """A exceção da D7: coleta que terminou SEM falha e mesmo assim não produziu
    o indicador significa que a fonte não tem esse dado para este país.
    Insistir não muda nada — o país sai declarado e os outros são entregues."""
    session, mocks = _patched_service(all_covered=False, missing_terms=[], related_exists=True)
    mocks["coverage_service"].check_or_collect = AsyncMock(return_value=(uuid.uuid4(), [_task("done")]))
    mocks["mos_engine"].find_incomplete_countries = AsyncMock(
        return_value={"USA": "Missing mandatory indicator(s): population_growth_score"}
    )
    dormiu = AsyncMock()
    try:
        with patch("app.services.analysis_service.asyncio.sleep", dormiu):
            service = AnalysisService(session)
            await service.run(_REQUEST_ID)
    finally:
        for key, value in list(mocks.items()):
            if key.startswith("_patcher_"):
                value.stop()

    dormiu.assert_not_awaited(), "causa definitiva não gera espera"
    mocks["analysis_repo"].mark_done.assert_awaited_once()
