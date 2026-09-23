"""Uploading is free and idempotent; running costs money and says so first."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from langfuse.api import NotFoundError

_spec = importlib.util.spec_from_file_location(
    "run_eval", Path(__file__).resolve().parents[1] / "scripts" / "run_eval.py"
)
run_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_eval)


class FakeLangfuse:
    """Stands in for Langfuse: the network is the only thing faked."""

    def __init__(
        self,
        existing_datasets: set[str] | None = None,
        score_configs: list[SimpleNamespace] | None = None,
    ) -> None:
        self.existing = set(existing_datasets or ())
        self.datasets: list[str] = []
        self.items: list[dict] = []
        self.score_configs: list[dict] = []
        self.archived: list[tuple[str, bool]] = []
        self.ran_experiment = False
        self.api = self._API(self, list(score_configs or []))

    def get_dataset(self, name: str):
        if name not in self.existing:
            raise NotFoundError(body={"message": "not found"})
        return object()

    def create_dataset(self, *, name: str, description: str | None = None, metadata=None) -> None:
        self.datasets.append(name)
        self.existing.add(name)

    def create_dataset_item(self, **item) -> None:
        self.items.append(item)

    def run_experiment(self, **kwargs: object) -> None:
        """Never legitimately reached by --upload or --archive-duplicates —
        present only so a test can prove neither mode falls through to it."""
        self.ran_experiment = True

    class _API:
        """Mirrors the installed SDK's shape: score configs are read, created
        and updated through client.api.score_configs.*, not a top-level
        client method."""

        def __init__(self, outer: "FakeLangfuse", remote_score_configs: list[SimpleNamespace]) -> None:
            self.score_configs = FakeLangfuse._ScoreConfigs(outer, remote_score_configs)

    class _ScoreConfigs:
        # Deliberately smaller than any list a test uses, so a test with more
        # than one page's worth of existing configs actually exercises the
        # page-until-total_pages loop in run_eval, not just a single-page call.
        _PAGE_SIZE = 2

        def __init__(self, outer: "FakeLangfuse", remote: list[SimpleNamespace]) -> None:
            self._outer = outer
            self._remote = remote

        def get(self, *, page: int = 1, limit: int | None = None) -> SimpleNamespace:
            size = self._PAGE_SIZE
            start = (page - 1) * size
            chunk = self._remote[start : start + size]
            total_pages = max(1, -(-len(self._remote) // size))
            return SimpleNamespace(
                data=chunk,
                meta=SimpleNamespace(page=page, limit=size, total_items=len(self._remote), total_pages=total_pages),
            )

        def create(self, **config) -> None:
            self._outer.score_configs.append(config)

        def update(self, config_id: str, *, is_archived: bool | None = None, **_rest: object) -> None:
            for existing in self._remote:
                if existing.id == config_id:
                    existing.is_archived = is_archived
            self._outer.archived.append((config_id, is_archived))


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


# --- F4: score configs are not idempotent by name in Langfuse -------------


def test_uploading_skips_score_configs_that_already_exist() -> None:
    """Langfuse minted a new id per create() even for a name it already held
    (F4) — --upload run twice created 13 duplicates. Listing what exists
    first, across every page, is what makes a second run a no-op."""
    already_there = [
        SimpleNamespace(id="cfg-1", name="markets_recall", created_at=1, is_archived=False),
        SimpleNamespace(id="cfg-2", name="must_include_hit", created_at=2, is_archived=False),
        SimpleNamespace(id="cfg-3", name="task_completion_rate", created_at=3, is_archived=False),
    ]  # 3 items over a fake page size of 2: exercises the multi-page loop
    fake = FakeLangfuse(score_configs=already_there)

    report = run_eval.upload(fake, run_eval.load_all(), run_eval.SCORE_CONFIGS)

    created_names = [c["name"] for c in fake.score_configs]
    assert "markets_recall" not in created_names
    assert "must_include_hit" not in created_names
    assert "task_completion_rate" not in created_names
    assert len(created_names) == len(run_eval.SCORE_CONFIGS) - 3
    assert any("score config markets_recall: exists" in line for line in report)
    assert any("score config must_include_hit: exists" in line for line in report)
    for name in created_names:
        assert any(f"score config {name}: created" in line for line in report)


def test_archive_duplicates_keeps_the_oldest_and_archives_the_rest(monkeypatch, capsys) -> None:
    older = SimpleNamespace(id="cfg-old", name="markets_recall", created_at=1, is_archived=False)
    newer = SimpleNamespace(id="cfg-new", name="markets_recall", created_at=2, is_archived=False)
    solo = SimpleNamespace(id="cfg-solo", name="must_include_hit", created_at=1, is_archived=False)
    fake = FakeLangfuse(score_configs=[older, newer, solo])
    monkeypatch.setattr(run_eval.observability, "client", lambda: fake)

    result = run_eval.main(["--archive-duplicates"])

    assert result == 0
    assert fake.archived == [("cfg-new", True)]
    assert newer.is_archived is True
    assert older.is_archived is False, "the oldest of a duplicate pair is kept, not archived"
    assert solo.is_archived is False, "a name with only one config has nothing to archive"
    assert "cfg-new" in capsys.readouterr().out


def test_archive_duplicates_is_its_own_mode_like_upload(monkeypatch) -> None:
    """Must win over a named dataset, need no --yes and no confirmation
    answer, and never reach the experiment path — proven here by never
    monkeypatching builtins.input: falling through to the confirmation
    prompt would blow up on captured stdin, not just this assertion."""
    fake = FakeLangfuse(score_configs=[])
    monkeypatch.setattr(run_eval.observability, "client", lambda: fake)

    result = run_eval.main(["identify-product", "--archive-duplicates"])

    assert result == 0
    assert fake.ran_experiment is False


def test_the_smoke_sample_is_spread_across_the_dataset() -> None:
    """Langfuse returns items newest-first, so taking the first five sampled one
    end of the file — for identify-product, three of the four no-product cases."""
    items = list(range(25))

    chosen = run_eval._spread(items, 5)

    assert len(chosen) == 5
    assert chosen == [0, 5, 10, 15, 20]
    assert run_eval._spread(items, 40) == items
