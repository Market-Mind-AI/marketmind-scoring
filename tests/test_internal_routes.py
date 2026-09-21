"""The tools the orchestrator drives an analysis with."""

import uuid
from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app

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
    with patch(
        "app.main.AnalysisResumeService.run_forever", AsyncMock(return_value=None)
    ), TestClient(app) as test_client:
        yield test_client


# --- these routes write, so they are not for the browser ---


def test_a_call_without_the_token_is_refused(client: TestClient) -> None:
    response = client.post("/internal/analyses", json={"countries": ["ESP"]})
    assert response.status_code == 401


def test_a_call_with_the_wrong_token_is_refused(client: TestClient) -> None:
    response = client.post(
        "/internal/analyses", json={"countries": ["ESP"]}, headers={"X-Internal-Token": "wrong"}
    )
    assert response.status_code == 401


def test_with_no_secret_configured_everything_is_refused(client: TestClient) -> None:
    """Fails closed. An unset secret that waved calls through would look, from
    outside a running deployment, exactly like one configured correctly."""
    settings.internal_api_token = ""
    response = client.post("/internal/analyses", json={"countries": ["ESP"]}, headers=_HEADERS)
    assert response.status_code == 503


# --- registering, reporting, finishing ---


def test_registering_an_analysis_returns_something_to_track_it_by(client: TestClient) -> None:
    response = client.post(
        "/internal/analyses",
        json={"countries": ["ESP", "PRT"], "canonical_title": "air fryer", "user_prompt": "vale a pena?"},
        headers=_HEADERS,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "pending"
    assert uuid.UUID(body["request_id"])


def test_progress_records_the_note_and_the_heartbeat(client: TestClient) -> None:
    """Two different readers. The note is for whoever is watching; the heartbeat
    is how a run that died is told apart from one that is merely slow."""
    request_id = client.post("/internal/analyses", json={"countries": ["ESP"]}, headers=_HEADERS).json()["request_id"]

    response = client.patch(
        f"/internal/analyses/{request_id}/progress",
        json={"message": "collecting ESP demand"},
        headers=_HEADERS,
    )

    assert response.status_code == 200
    assert response.json()["progress"] == "collecting ESP demand"


def test_an_empty_progress_note_still_counts_as_a_sign_of_life(client: TestClient) -> None:
    request_id = client.post("/internal/analyses", json={"countries": ["ESP"]}, headers=_HEADERS).json()["request_id"]

    response = client.patch(f"/internal/analyses/{request_id}/progress", json={"message": None}, headers=_HEADERS)

    assert response.status_code == 200
    assert response.json()["progress"] is None


def test_completing_requires_at_least_one_scored_market(client: TestClient) -> None:
    """Billing is tied to this state, so completing with nothing is how a buyer
    ends up paying for an empty answer."""
    request_id = client.post("/internal/analyses", json={"countries": ["ESP"]}, headers=_HEADERS).json()["request_id"]

    response = client.post(f"/internal/analyses/{request_id}/complete", json={"result": []}, headers=_HEADERS)

    assert response.status_code == 422


def test_completing_with_markets_marks_it_done(client: TestClient) -> None:
    request_id = client.post("/internal/analyses", json={"countries": ["ESP"]}, headers=_HEADERS).json()["request_id"]

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
    request_id = client.post("/internal/analyses", json={"countries": ["ESP"]}, headers=_HEADERS).json()["request_id"]
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
    request_id = client.post("/internal/analyses", json={"countries": ["ESP"]}, headers=_HEADERS).json()["request_id"]

    response = client.post(f"/internal/analyses/{request_id}/fail", json={"error_message": ""}, headers=_HEADERS)

    assert response.status_code == 422


def test_failing_with_a_reason_records_it(client: TestClient) -> None:
    request_id = client.post("/internal/analyses", json={"countries": ["ESP"]}, headers=_HEADERS).json()["request_id"]

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
    response = client.patch(
        f"/internal/analyses/{uuid.uuid4()}/progress", json={"message": "x"}, headers=_HEADERS
    )
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
    response = client.post(
        "/internal/evidence",
        json={"entity_key": str(uuid.uuid4()), "countries": ["ESP"], "period_key": "2026-09"},
        headers=_HEADERS,
    )

    assert response.status_code == 200
    assert response.json()["packet"] is None


def test_narrate_answers_null_when_the_model_fails(client: TestClient) -> None:
    """Best-effort by design: the numbers are the answer, the prose is a reading
    of them, and a missing reading never invalidates the answer."""
    with patch(
        "app.services.explanation_client.ExplanationClient.narrate", AsyncMock(return_value=None)
    ):
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
    request_id = client.post(
        "/internal/analyses",
        json={"countries": ["ESP", "PRT"], "canonical_title": "air fryer", "user_prompt": "vale a pena?"},
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
    with patch(
        "app.services.analysis_service.AnalysisService.ensure_product_enrichment", AsyncMock(return_value=None)
    ):
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
    with patch(
        "app.services.analysis_service.AnalysisService.ensure_coverage", AsyncMock(return_value=[])
    ):
        response = client.post(
            "/internal/coverage/ensure", json={"country_iso3": "ESP"}, headers=_HEADERS
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
    client.post(
        "/internal/champion",
        json={"entity_key": entity_key, "period_key": period, "country": "BRA", "mos": 66.4},
        headers=_HEADERS,
    )

    collected = AsyncMock(return_value=None)
    with patch("app.services.analysis_service.AnalysisService.ensure_candidates_coverage", collected):
        response = client.post(
            "/internal/candidates/coverage",
            json={"entity_key": entity_key, "requested_countries": ["ESP"], "period_key": period},
            headers=_HEADERS,
        )

    assert response.json()["champion_cached"] is True
    collected.assert_not_awaited()


def test_candidate_collection_runs_when_there_is_no_cached_champion(client: TestClient) -> None:
    entity_key = _resolve(client, f"fresh champion {uuid.uuid4().hex[:6]}")

    collected = AsyncMock(return_value=None)
    with patch("app.services.analysis_service.AnalysisService.ensure_candidates_coverage", collected):
        response = client.post(
            "/internal/candidates/coverage",
            json={"entity_key": entity_key, "requested_countries": ["ESP"], "period_key": "2026-09"},
            headers=_HEADERS,
        )

    assert response.json()["champion_cached"] is False
    collected.assert_awaited_once()


def test_a_champion_can_be_stored_as_nothing_scorable(client: TestClient) -> None:
    """A real outcome that has to be recorded: without it, the same fruitless
    candidate work is attempted again on every later analysis."""
    entity_key = _resolve(client, f"nothing scorable {uuid.uuid4().hex[:6]}")

    response = client.post(
        "/internal/champion",
        json={"entity_key": entity_key, "period_key": "2026-09", "country": None, "mos": None},
        headers=_HEADERS,
    )

    assert response.status_code == 200
    cached = client.post(
        "/internal/candidates/coverage",
        json={"entity_key": entity_key, "requested_countries": ["ESP"], "period_key": "2026-09"},
        headers=_HEADERS,
    )
    assert cached.json()["champion_cached"] is True


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


def test_scoring_candidates_reports_the_ones_it_could_not_score(client: TestClient) -> None:
    """Excluded is reported with its reason, never omitted — a country missing
    from the answer is indistinguishable from one that was never tried."""
    entity_key = _resolve(client, f"candidates {uuid.uuid4().hex[:6]}")
    from types import SimpleNamespace

    rows = [
        SimpleNamespace(country="BRA", mos=66.4, availability_status="available", excluded_reason=None),
        SimpleNamespace(
            country="PER",
            mos=None,
            availability_status="unavailable",
            excluded_reason="Missing mandatory indicator(s): trend_score",
        ),
    ]
    with patch(
        "app.services.analysis_service.AnalysisService.score_reference_candidates",
        AsyncMock(return_value=rows),
    ):
        response = client.post(
            "/internal/candidates/score",
            json={"entity_key": entity_key, "requested_countries": ["ESP"], "period_key": "2026-09"},
            headers=_HEADERS,
        )

    candidates = response.json()["candidates"]
    assert [c["country"] for c in candidates] == ["BRA", "PER"]
    assert candidates[1]["mos"] is None
    assert "trend_score" in candidates[1]["excluded_reason"]


def test_every_judgement_tool_needs_the_token(client: TestClient) -> None:
    for path, payload in [
        ("/internal/enrich-terms", {"entity_key": str(uuid.uuid4()), "countries": ["ESP"]}),
        ("/internal/coverage/ensure", {"country_iso3": "ESP"}),
        ("/internal/demand-ceiling", {"entity_key": str(uuid.uuid4()), "countries": ["ESP"], "period_key": "x"}),
        ("/internal/champion", {"entity_key": str(uuid.uuid4()), "period_key": "x"}),
    ]:
        assert client.post(path, json=payload).status_code == 401, path
