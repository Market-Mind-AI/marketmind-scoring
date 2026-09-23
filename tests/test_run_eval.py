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
