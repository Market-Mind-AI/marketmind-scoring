"""The tools the orchestrator drives an analysis with."""

import uuid
from collections.abc import Generator
from contextlib import ExitStack
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, PropertyMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.services.catalog_service import CatalogService

_TOKEN = "test-internal-token"
_HEADERS = {"X-Internal-Token": _TOKEN}


@pytest.fixture(autouse=True)
def _token() -> Generator[None, None, None]:
    original = settings.internal_api_token
    settings.internal_api_token = _TOKEN
    yield
    settings.internal_api_token = original


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    """One client, one event loop, for a whole test.

    These tests drive a sequence — register, report, complete, read back — and a
    plain TestClient opens a fresh loop per request, which invalidates the
    pooled database connection the previous request checked out.

    The lifespan is entered to get that single loop, so the resume sweep is
    stubbed out: it exists to pick up analyses that died with their process,
    and left running it would do exactly that to the ones these tests create.
    """
    with (
        patch("app.main.AnalysisResumeService.run_forever", AsyncMock(return_value=None)),
        TestClient(app) as test_client,
    ):
        yield test_client


def _registrar(client: TestClient, corpo: dict, headers: dict | None = None) -> Any:
    """Registra uma análise pelo único caminho que existe, sem rede: o LLM do
    match e os vetores ficam desligados. `catalog_method` e
    `catalog_match_score`, quando o teste é sobre eles, viram o que a resolução
    responde — é de lá que o registro os tira."""
    corpo = dict(corpo)
    corpo.setdefault("product_name", corpo.pop("canonical_title", None) or f"produto {uuid.uuid4().hex[:8]}")
    metodo = corpo.pop("catalog_method", None)
    score = corpo.pop("catalog_match_score", None)
    with ExitStack() as pilha:
        pilha.enter_context(patch("app.services.catalog_service.CatalogMatcher.pick", AsyncMock(return_value=None)))
        pilha.enter_context(
            patch("app.services.catalog_service.EmbeddingClient.enabled", new_callable=PropertyMock, return_value=False)
        )
        if metodo:
            resolver = CatalogService.resolve

            async def _resolve(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
                resolvido = await resolver(self, *args, **kwargs)
                return resolvido.model_copy(update={"method": metodo, "match_score": score})

            pilha.enter_context(patch("app.services.catalog_service.CatalogService.resolve", _resolve))
        return client.post("/internal/analyses/register", json=corpo, headers=headers)


# --- these routes write, so they are not for the browser ---


def test_a_call_without_the_token_is_refused(client: TestClient) -> None:
    response = _registrar(client, {"countries": ["ESP"]})
    assert response.status_code == 401


def test_a_call_with_the_wrong_token_is_refused(client: TestClient) -> None:
    response = _registrar(client, {"countries": ["ESP"]}, headers={"X-Internal-Token": "wrong"})
    assert response.status_code == 401


def test_with_no_secret_configured_everything_is_refused(client: TestClient) -> None:
    """Fails closed. An unset secret that waved calls through would look, from
    outside a running deployment, exactly like one configured correctly."""
    settings.internal_api_token = ""
    response = _registrar(client, {"countries": ["ESP"]}, headers=_HEADERS)
    assert response.status_code == 503


# --- registering, reporting, finishing ---


def test_registering_an_analysis_returns_something_to_track_it_by(client: TestClient) -> None:
    response = _registrar(
        client,
        {"countries": ["ESP", "PRT"], "canonical_title": "air fryer", "user_prompt": "vale a pena?"},
        headers=_HEADERS,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "pending"
    assert uuid.UUID(body["request_id"])


def test_how_the_catalogue_matched_survives_to_the_resumed_run(client: TestClient) -> None:
    """O agente grava isto ao registrar e lê de volta ao começar o grafo, porque
    é lá que existe trace para anexar o fato — e porque uma retomada não resolve
    o produto outra vez. Sem o campo voltar na leitura, o trace diria "não sei"
    de algo que se soube, e nada acusaria: o fio inteiro é silencioso.
    """
    request_id = _registrar(
        client,
        {
            "countries": ["ESP"],
            "canonical_title": "Picanha Swift",
            "catalog_method": "trigram",
            "catalog_match_score": 0.87,
        },
        headers=_HEADERS,
    ).json()["request_id"]

    lido = client.get(f"/internal/analyses/{request_id}", headers=_HEADERS).json()

    assert lido["catalog_method"] == "trigram"
    assert float(lido["catalog_match_score"]) == 0.87


def test_an_analysis_registered_without_the_catalogue_fact_reads_back_empty(client: TestClient) -> None:
    """Nulo, não zero: `catalog_match_score` só existe para `trigram`, que é o
    único passo que decide por número. Zero ali seria "não se parecia nada",
    que é uma afirmação diferente de "não se aplica"."""
    request_id = _registrar(client, {"countries": ["ESP"], "catalog_method": "created"}, headers=_HEADERS).json()[
        "request_id"
    ]

    lido = client.get(f"/internal/analyses/{request_id}", headers=_HEADERS).json()

    assert lido["catalog_method"] == "created"
    assert lido["catalog_match_score"] is None


def test_progress_records_the_note_and_the_heartbeat(client: TestClient) -> None:
    """Two different readers. The note is for whoever is watching; the heartbeat
    is how a run that died is told apart from one that is merely slow."""
    request_id = _registrar(client, {"countries": ["ESP"]}, headers=_HEADERS).json()["request_id"]

    response = client.patch(
        f"/internal/analyses/{request_id}/progress",
        json={"message": "collecting ESP demand"},
        headers=_HEADERS,
    )

    assert response.status_code == 200
    assert response.json()["progress"] == "collecting ESP demand"


def test_an_empty_progress_note_still_counts_as_a_sign_of_life(client: TestClient) -> None:
    request_id = _registrar(client, {"countries": ["ESP"]}, headers=_HEADERS).json()["request_id"]

    response = client.patch(f"/internal/analyses/{request_id}/progress", json={"message": None}, headers=_HEADERS)

    assert response.status_code == 200
    assert response.json()["progress"] is None


def test_completing_requires_at_least_one_scored_market(client: TestClient) -> None:
    """Billing is tied to this state, so completing with nothing is how a buyer
    ends up paying for an empty answer."""
    request_id = _registrar(client, {"countries": ["ESP"]}, headers=_HEADERS).json()["request_id"]

    response = client.post(f"/internal/analyses/{request_id}/complete", json={"result": []}, headers=_HEADERS)

    assert response.status_code == 422


def test_completing_with_markets_marks_it_done(client: TestClient) -> None:
    request_id = _registrar(client, {"countries": ["ESP"]}, headers=_HEADERS).json()["request_id"]

    response = client.post(
        f"/internal/analyses/{request_id}/complete",
        json={"result": [{"country": "ESP", "mos": 70.0}], "breakdown": {"countries": []}},
        headers=_HEADERS,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "done"


def test_the_narrative_is_attached_after_the_answer_lands(client: TestClient) -> None:
    """Separate calls on purpose: a single model call once took longer than the
    whole rest of the analysis, so the scores are delivered without it."""
    request_id = _registrar(client, {"countries": ["ESP"]}, headers=_HEADERS).json()["request_id"]
    client.post(
        f"/internal/analyses/{request_id}/complete",
        json={"result": [{"country": "ESP", "mos": 70.0}]},
        headers=_HEADERS,
    )

    response = client.post(
        f"/internal/analyses/{request_id}/narrative", json={"narrative": "Spain leads."}, headers=_HEADERS
    )

    assert response.status_code == 200
    stored = client.get(f"/internal/analyses/{request_id}", headers=_HEADERS).json()
    assert stored["explanation"] == "Spain leads."
    assert stored["status"] == "done"


def test_failing_requires_a_reason(client: TestClient) -> None:
    request_id = _registrar(client, {"countries": ["ESP"]}, headers=_HEADERS).json()["request_id"]

    response = client.post(f"/internal/analyses/{request_id}/fail", json={"error_message": ""}, headers=_HEADERS)

    assert response.status_code == 422


def test_failing_with_a_reason_records_it(client: TestClient) -> None:
    request_id = _registrar(client, {"countries": ["ESP"]}, headers=_HEADERS).json()["request_id"]

    response = client.post(
        f"/internal/analyses/{request_id}/fail",
        json={"error_message": "UN Data unreachable for 24h"},
        headers=_HEADERS,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    stored = client.get(f"/internal/analyses/{request_id}", headers=_HEADERS).json()
    assert stored["error_message"] == "UN Data unreachable for 24h"


def test_an_unknown_analysis_is_a_404(client: TestClient) -> None:
    response = client.patch(f"/internal/analyses/{uuid.uuid4()}/progress", json={"message": "x"}, headers=_HEADERS)
    assert response.status_code == 404


# --- weights, evidence, narrative ---


def test_calibrating_weights_is_safe_to_repeat(client: TestClient) -> None:
    """Deterministic — Rank Reciprocal over a fixed priority order, reading no
    country data — so the agent can call it before every scoring."""
    period = f"test-{uuid.uuid4().hex[:8]}"

    first = client.post("/internal/weights/calibrate", json={"period_key": period}, headers=_HEADERS)
    second = client.post("/internal/weights/calibrate", json={"period_key": period}, headers=_HEADERS)

    assert first.status_code == 200
    assert first.json()["weights"] == second.json()["weights"]
    assert sum(first.json()["weights"].values()) == pytest.approx(1.0)


def test_evidence_is_null_when_no_country_has_a_score(client: TestClient) -> None:
    """Absent is reported as absent. An empty packet would read downstream as
    'nothing to say about this market', which is a different claim."""
    entity_key = _resolve(client, f"sem score {uuid.uuid4().hex[:6]}")
    response = client.post(
        "/internal/evidence",
        json={"entity_key": entity_key, "countries": ["ESP"], "period_key": "2026-09"},
        headers=_HEADERS,
    )

    assert response.status_code == 200
    assert response.json()["packet"] is None


def test_narrate_answers_null_when_the_model_fails(client: TestClient) -> None:
    """Best-effort by design: the numbers are the answer, the prose is a reading
    of them, and a missing reading never invalidates the answer."""
    with patch("app.services.explanation_client.ExplanationClient.narrate", AsyncMock(return_value=None)):
        response = client.post(
            "/internal/narrate",
            json={"packet": {"countries": []}, "product_name": "air fryer"},
            headers=_HEADERS,
        )

    assert response.status_code == 200
    assert response.json()["narrative"] is None


def test_reading_an_analysis_back_gives_a_resumed_run_what_it_needs(client: TestClient) -> None:
    """Resuming is re-running, so the only thing that must not be re-decided is
    what was already decided: the product, the countries, the buyer's question."""
    request_id = _registrar(
        client,
        {"countries": ["ESP", "PRT"], "canonical_title": "air fryer", "user_prompt": "vale a pena?"},
        headers=_HEADERS,
    ).json()["request_id"]

    stored = client.get(f"/internal/analyses/{request_id}", headers=_HEADERS).json()

    assert stored["countries"] == ["ESP", "PRT"]
    assert stored["canonical_title"] == "air fryer"
    assert stored["user_prompt"] == "vale a pena?"
    assert stored["status"] == "pending"


# --- the steps that carry the analysis's own judgement ---


def _resolve(client: TestClient, name: str) -> str:
    response = client.post("/catalog/resolve", json={"text": name, "source": "test"})
    assert response.status_code == 200, response.text
    return response.json()["entity_key"]


def test_enrich_terms_needs_a_real_entity(client: TestClient) -> None:
    response = client.post(
        "/internal/enrich-terms",
        json={"entity_key": str(uuid.uuid4()), "countries": ["ESP"]},
        headers=_HEADERS,
    )
    assert response.status_code == 404


def test_enrich_terms_reports_which_countries_have_vocabulary(client: TestClient) -> None:
    """Only what is missing is generated, so an entity that already has terms
    costs no model call — reuse is what makes a repeated analysis cheap."""
    entity_key = _resolve(client, f"air fryer {uuid.uuid4().hex[:6]}")
    with patch("app.services.analysis_service.AnalysisService.ensure_product_enrichment", AsyncMock(return_value=None)):
        response = client.post(
            "/internal/enrich-terms",
            json={"entity_key": entity_key, "countries": ["ESP", "PRT"]},
            headers=_HEADERS,
        )

    assert response.status_code == 200
    assert response.json()["entity_key"] == entity_key


def test_ensure_coverage_returns_the_tasks_it_dispatched(client: TestClient) -> None:
    """The task list is the point: it is how "the source is down, insist" is
    told apart from "the source has no such datum, declare it"."""
    with patch("app.services.analysis_service.AnalysisService.ensure_coverage", AsyncMock(return_value=[])):
        response = client.post(
            "/internal/coverage/ensure", json={"country_iso3": "ESP", "period_key": "2026-10"}, headers=_HEADERS
        )

    assert response.status_code == 200
    body = response.json()
    assert body["country"] == "ESP"
    assert body["tasks"] == []


def test_ensure_coverage_rejects_a_country_that_is_not_iso3(client: TestClient) -> None:
    response = client.post("/internal/coverage/ensure", json={"country_iso3": "ES"}, headers=_HEADERS)
    assert response.status_code == 422


def test_candidate_collection_is_skipped_when_the_champion_is_cached(client: TestClient) -> None:
    """Candidate demand is the slowest collection in the system and re-running
    it produces the same answer."""
    entity_key = _resolve(client, f"cached champion {uuid.uuid4().hex[:6]}")
    period = "2026-09"
    # O campeão guardado vale para a régua com que foi calculado; aqui, a de agora.
    guardado = AsyncMock(return_value=(True, "BRA", Decimal("66.4")))

    collected = AsyncMock(return_value=None)
    with (
        patch("app.services.analysis_service.AnalysisService.ensure_candidates_coverage", collected),
        patch("app.repositories.product_repository.ProductRepository.champion_for", guardado),
    ):
        response = client.post(
            "/internal/candidates/coverage",
            json={"entity_key": entity_key, "requested_countries": ["ESP"], "period_key": period},
            headers=_HEADERS,
        )

    assert response.json()["champion_cached"] is True
    collected.assert_not_awaited()


def test_candidate_collection_runs_when_there_is_no_cached_champion(client: TestClient) -> None:
    entity_key = _resolve(client, f"fresh champion {uuid.uuid4().hex[:6]}")

    collected = AsyncMock(return_value=[])
    with patch("app.services.analysis_service.AnalysisService.ensure_candidates_coverage", collected):
        response = client.post(
            "/internal/candidates/coverage",
            json={"entity_key": entity_key, "requested_countries": ["ESP"], "period_key": "2026-09"},
            headers=_HEADERS,
        )

    assert response.json()["champion_cached"] is False
    collected.assert_awaited_once()


def test_candidate_collection_reports_what_it_did(client: TestClient) -> None:
    """É o passo MAIS CARO do sistema — três análises seguidas de um produto
    levaram 594s, 203s e 396s, quase todo o repetido sendo trabalho de
    candidato refeito — e era o único que não devolvia relato nenhum. Sem isto
    ele não aparece no trace, e uma regressão de custo ali é invisível."""
    entity_key = _resolve(client, f"relato de candidato {uuid.uuid4().hex[:6]}")

    async def _coleta(self, entity, requested, *, period_key, marca=None, reuse_report=None):  # noqa: ANN001, ANN202
        if reuse_report is not None:
            reuse_report["reused"] = {"pytrends": 12}
            reuse_report["related_fresh"] = 2
        return []

    with patch("app.services.analysis_service.AnalysisService.ensure_candidates_coverage", _coleta):
        body = client.post(
            "/internal/candidates/coverage",
            json={"entity_key": entity_key, "requested_countries": ["ESP"], "period_key": "2026-09"},
            headers=_HEADERS,
        ).json()

    assert body["reused"] == {"pytrends": 12}
    assert body["related_fresh"] == 2
    assert body["tasks"] == []


def test_crowning_answers_the_champion_and_its_row_when_it_was_not_requested(client: TestClient) -> None:
    from app.services.analysis_service import CrownedChampion

    entity_key = _resolve(client, f"coroa {uuid.uuid4().hex[:6]}")
    linha = SimpleNamespace(
        id=uuid.uuid4(), entity_key=uuid.UUID(entity_key), country="USA", period_type="month", period_key="2026-10",
        mos=Decimal("71.2"), mos_before_cap=None, capped_by=None, topsis_score=None, library_topsis_score=None,
        library_mos=None, mos_interpretation="Good Opportunity", explanation=None, availability_status="available",
        excluded_reason=None, computed_at=datetime(2026, 10, 26, tzinfo=UTC),
    )  # fmt: skip
    coroado = CrownedChampion("USA", 71.2, False, linha, False, 15800.0)
    with patch("app.services.analysis_service.AnalysisService.crown_champion", AsyncMock(return_value=coroado)):
        body = client.post(
            "/internal/champion/crown",
            json={
                "entity_key": entity_key,
                "period_key": "2026-10",
                "requested_countries": ["BRA"],
                "requested_results": [{"country": "BRA", "mos": 64.0}],
            },
            headers=_HEADERS,
        ).json()

    assert (body["champion_country"], body["requested"], body["demand_ceiling"]) == ("USA", False, 15800.0)
    assert body["champion_row"]["country"] == "USA"


def test_the_demand_ceiling_reports_which_ruler_was_used(client: TestClient) -> None:
    """A normalised figure presented as an absolute one is a false claim, so
    the narrative has to be able to say which ruler it is reading."""
    entity_key = _resolve(client, f"ruler {uuid.uuid4().hex[:6]}")
    with patch(
        "app.services.analysis_service.AnalysisService.resolve_demand_ceiling",
        AsyncMock(return_value=(1_500_000.0, {"mode": "reference_candidate", "country": "BRA"})),
    ):
        response = client.post(
            "/internal/demand-ceiling",
            json={"entity_key": entity_key, "countries": ["ESP"], "period_key": "2026-09"},
            headers=_HEADERS,
        )

    body = response.json()
    assert body["demand_ceiling"] == 1_500_000.0
    assert body["demand_ruler"]["mode"] == "reference_candidate"


def test_every_judgement_tool_needs_the_token(client: TestClient) -> None:
    for path, payload in [
        ("/internal/enrich-terms", {"entity_key": str(uuid.uuid4()), "countries": ["ESP"]}),
        ("/internal/coverage/ensure", {"country_iso3": "ESP"}),
        ("/internal/demand-ceiling", {"entity_key": str(uuid.uuid4()), "countries": ["ESP"], "period_key": "x"}),
        ("/internal/champion/crown", {"entity_key": str(uuid.uuid4()), "period_key": "x"}),
    ]:
        assert client.post(path, json=payload).status_code == 401, path


def test_a_match_near_the_threshold_is_reported_as_borderline(client: TestClient) -> None:
    """O limiar mora neste serviço, então quem rotula é ele. O agente chegou a
    repetir o 0.4 do lado dele só para escrever "BORDERLINE", e número repetido
    envelhece sozinho: mudar o limiar aqui deixaria o rótulo de lá mentindo sem
    nada acusar."""
    request_id = _registrar(
        client, {"countries": ["ESP"], "catalog_method": "trigram", "catalog_match_score": 0.41}, headers=_HEADERS
    ).json()["request_id"]

    lido = client.get(f"/internal/analyses/{request_id}", headers=_HEADERS).json()

    assert lido["catalog_match_borderline"] is True


def test_a_comfortable_match_is_not_borderline(client: TestClient) -> None:
    request_id = _registrar(
        client, {"countries": ["ESP"], "catalog_method": "trigram", "catalog_match_score": 0.87}, headers=_HEADERS
    ).json()["request_id"]

    assert client.get(f"/internal/analyses/{request_id}", headers=_HEADERS).json()["catalog_match_borderline"] is False


def test_a_match_with_no_score_is_not_borderline(client: TestClient) -> None:
    """`alias`, `llm` e `created` não têm score — não há de que estar perto."""
    request_id = _registrar(client, {"countries": ["ESP"], "catalog_method": "alias"}, headers=_HEADERS).json()[
        "request_id"
    ]

    assert client.get(f"/internal/analyses/{request_id}", headers=_HEADERS).json()["catalog_match_borderline"] is False


# --- the narrative comes out in the buyer's language ---
#
# The interface is EN/ES and the configured default was PT, so a reader who
# chose Spanish got a Portuguese explanation. The language is the buyer's
# choice, not the service's: it is registered with the analysis and read back
# when the run narrates.


def test_the_language_asked_for_is_stored_and_read_back(client: TestClient) -> None:
    request_id = _registrar(client, {"countries": ["ESP"], "language": "es"}, headers=_HEADERS).json()["request_id"]

    lido = client.get(f"/internal/analyses/{request_id}", headers=_HEADERS).json()

    assert lido["language"] == "es"


def test_an_analysis_registered_without_a_language_reads_back_null(client: TestClient) -> None:
    """Null is "the configured default" — what every analysis got before the
    language travelled with the request."""
    request_id = _registrar(client, {"countries": ["ESP"]}, headers=_HEADERS).json()["request_id"]

    lido = client.get(f"/internal/analyses/{request_id}", headers=_HEADERS).json()

    assert lido["language"] is None


def test_a_language_the_narrative_has_no_texts_for_is_refused(client: TestClient) -> None:
    response = _registrar(client, {"countries": ["ESP"], "language": "fr"}, headers=_HEADERS)

    assert response.status_code == 422


def test_narrate_stores_the_text_while_it_is_written_and_drops_it_if_it_fails(client: TestClient) -> None:
    """Com a análise dona do texto, os pedaços vão para ela enquanto o modelo
    escreve. Se o modelo falha no meio, o pedaço não fica como explicação."""

    async def narrar_e_falhar(_self, **kwargs):  # noqa: ANN001, ANN003, ANN202
        await kwargs["on_text"]("Espanha li")
        return None

    stored = AsyncMock()
    with (
        patch("app.services.explanation_client.ExplanationClient.narrate", narrar_e_falhar),
        patch("app.api.routes.internal._PartialNarrative._store", stored),
    ):
        response = client.post(
            "/internal/narrate",
            json={"packet": {"countries": []}, "product_name": "air fryer", "request_id": str(uuid.uuid4())},
            headers=_HEADERS,
        )

    assert response.status_code == 200
    assert [call.args[0] for call in stored.await_args_list] == ["Espanha li", None]


def test_narrate_writes_in_the_language_the_analysis_asked_for(client: TestClient) -> None:
    narrate = AsyncMock(return_value="España lidera.")
    with patch("app.services.explanation_client.ExplanationClient.narrate", narrate):
        response = client.post(
            "/internal/narrate",
            json={"packet": {"countries": []}, "product_name": "air fryer", "language": "es"},
            headers=_HEADERS,
        )

    assert response.status_code == 200
    assert narrate.await_args.kwargs["language"] == "es"


def test_running_accepts_the_deadline(client: TestClient) -> None:
    request_id = _registrar(client, {"countries": ["ESP"], "canonical_title": "prazo"}, headers=_HEADERS).json()[
        "request_id"
    ]
    resposta = client.post(
        f"/internal/analyses/{request_id}/running", json={"deadline_at": "2026-10-27T12:00:00+00:00"}, headers=_HEADERS
    )

    assert resposta.status_code == 200
    assert resposta.json()["status"] == "running"


def test_running_without_a_body_still_works(client: TestClient) -> None:
    request_id = _registrar(client, {"countries": ["ESP"], "canonical_title": "sem prazo"}, headers=_HEADERS).json()[
        "request_id"
    ]

    assert client.post(f"/internal/analyses/{request_id}/running", headers=_HEADERS).json()["status"] == "running"
