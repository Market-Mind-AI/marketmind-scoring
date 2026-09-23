"""Uploading is free and idempotent; running costs money and says so first."""

import importlib.util
from pathlib import Path

import pytest
from langfuse.api import NotFoundError

_spec = importlib.util.spec_from_file_location(
    "run_eval", Path(__file__).resolve().parents[1] / "scripts" / "run_eval.py"
)
run_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_eval)


class FakeLangfuse:
    """Stands in for Langfuse: the network is the only thing faked."""

    def __init__(self, existing_datasets: set[str] | None = None) -> None:
        self.existing = set(existing_datasets or ())
        self.datasets: list[str] = []
        self.items: list[dict] = []
        self.score_configs: list[dict] = []
        self.api = self._API(self)

    def get_dataset(self, name: str):
        if name not in self.existing:
            raise NotFoundError(body={"message": "not found"})
        return object()

    def create_dataset(self, *, name: str, description: str | None = None, metadata=None) -> None:
        self.datasets.append(name)
        self.existing.add(name)

    def create_dataset_item(self, **item) -> None:
        self.items.append(item)

    class _API:
        """Mirrors the installed SDK's shape: score configs are created through
        client.api.score_configs.create(...), not a top-level client method."""

        def __init__(self, outer: "FakeLangfuse") -> None:
            self.score_configs = FakeLangfuse._ScoreConfigs(outer)

    class _ScoreConfigs:
        def __init__(self, outer: "FakeLangfuse") -> None:
            self._outer = outer

        def create(self, **config) -> None:
            self._outer.score_configs.append(config)


def test_uploading_creates_missing_datasets_and_every_item() -> None:
    fake = FakeLangfuse(existing_datasets={"catalog-match"})

    report = run_eval.upload(fake, run_eval.load_all(), run_eval.SCORE_CONFIGS)

    assert fake.datasets == ["identify-product", "reference-markets"]
    assert len(fake.items) == 25 + 15 + 12
    first = fake.items[0]
    assert first["dataset_name"] == "identify-product"
    assert first["id"]
    assert set(first) >= {"dataset_name", "id", "input", "expected_output"}
    assert any("identify-product: created" in line for line in report)
    assert any("catalog-match: exists" in line for line in report)


def test_uploading_creates_the_score_configs() -> None:
    fake = FakeLangfuse()

    run_eval.upload(fake, run_eval.load_all(), run_eval.SCORE_CONFIGS)

    names = [c["name"] for c in fake.score_configs]
    assert "markets_recall" in names
    assert "task_completion_rate" in names
    assert all(c["data_type"] == "NUMERIC" for c in fake.score_configs)


def test_the_estimate_uses_the_measured_per_call_cost() -> None:
    assert run_eval.estimate("identify-product", 25) == pytest.approx(25 * run_eval.COST_PER_CASE["identify-product"])
    assert run_eval.estimate("catalog-match", 5) == pytest.approx(5 * run_eval.COST_PER_CASE["catalog-match"])


def test_a_run_without_langfuse_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(run_eval.observability, "client", lambda: None)

    assert run_eval.main(["identify-product", "--yes"]) == 1


def test_uploading_without_langfuse_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(run_eval.observability, "client", lambda: None)

    assert run_eval.main(["--upload"]) == 1


def test_a_run_stops_when_the_user_does_not_confirm(monkeypatch) -> None:
    monkeypatch.setattr(run_eval.observability, "client", lambda: object())
    monkeypatch.setattr("builtins.input", lambda *_: "n")

    assert run_eval.main(["catalog-match"]) == 2


class _FakeExperimentDataset:
    """10 items — enough to tell "the smoke default" (5) apart from "--full" (all of them)."""

    def __init__(self) -> None:
        self.items = [{"input": {}, "expected_output": {}} for _ in range(10)]


class _FakeExperimentResult:
    def format(self, **_: object) -> str:
        return ""


class _FakeExperimentClient:
    """DatasetClient.run_experiment (client.get_dataset(name).run_experiment) has
    no `data` parameter and always runs every item — the bug in F2. This fake
    stands in for the top-level client instead, whose run_experiment does take
    `data`, so a test can see exactly how many items a run actually passed."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def get_dataset(self, name: str) -> _FakeExperimentDataset:
        return _FakeExperimentDataset()

    def run_experiment(self, **kwargs: object) -> _FakeExperimentResult:
        self.calls.append(kwargs)
        return _FakeExperimentResult()


def _skip_prompt_warming(monkeypatch, loaded: int = 0) -> None:
    """main() warms prompts through asyncio.run(asyncio.to_thread(...)). The
    real asyncio.run() resets the process' current event loop when it
    returns, which corrupts the session-scoped loop tests/conftest.py shares
    across every async test in the suite — breaking unrelated async tests
    that happen to run later in the same pytest session. Closing the
    coroutine instead of running it keeps a test that reaches this line from
    ever calling the real asyncio.run()."""

    def _fake_run(coro):
        coro.close()
        return loaded

    monkeypatch.setattr(run_eval.asyncio, "run", _fake_run)


def test_the_smoke_limit_reaches_the_sdk_through_run_experiments_data(monkeypatch) -> None:
    fake = _FakeExperimentClient()
    monkeypatch.setattr(run_eval.observability, "client", lambda: fake)
    _skip_prompt_warming(monkeypatch)

    assert run_eval.main(["identify-product", "--yes"]) == 0

    assert len(fake.calls[-1]["data"]) == 5


def test_full_passes_every_remote_item_to_run_experiment(monkeypatch) -> None:
    fake = _FakeExperimentClient()
    monkeypatch.setattr(run_eval.observability, "client", lambda: fake)
    _skip_prompt_warming(monkeypatch)

    assert run_eval.main(["identify-product", "--full", "--yes"]) == 0

    assert len(fake.calls[-1]["data"]) == 10


def test_a_run_refuses_when_no_prompt_carries_a_non_production_label(monkeypatch, capsys) -> None:
    """warm() confirming 0 prompts under a non-production label means every
    call would silently fall back to the code text (F3): the run must not
    start, or two labels would score identically for the wrong reason."""
    monkeypatch.setattr(run_eval.observability, "client", lambda: object())
    _skip_prompt_warming(monkeypatch, loaded=0)

    result = run_eval.main(["identify-product", "--label", "candidate", "--yes"])

    assert result == 3
    assert "candidate" in capsys.readouterr().err
