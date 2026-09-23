"""Run a dataset of cases against the code as it stands, as a Langfuse experiment.

    python scripts/run_eval.py --upload                       # datasets + score configs (free)
    python scripts/run_eval.py identify-product               # 5 cases against production
    python scripts/run_eval.py identify-product --full        # every case
    python scripts/run_eval.py reference-markets --label candidate --full
    python scripts/run_eval.py --all --full

Only a run costs money: one model call per case. The script says how much it
expects to spend and asks before spending it.
"""

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langfuse.api import NotFoundError  # noqa: E402

from app import observability, prompts  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.prompt_registry import PROMPTS  # noqa: E402
from evaluation import cases, evaluators, tasks  # noqa: E402

SMOKE_CASES = 5

# Measured per-call cost on claude-opus-5 (2026-09-22), used only to warn before spending.
COST_PER_CASE: dict[str, float] = {
    "identify-product": 0.045,
    "catalog-match": 0.011,
    "reference-markets": 0.073,
}

EVALUATORS = {
    "identify-product": (
        [evaluators.identification],
        [evaluators.mean_of("product_found_correct"), evaluators.mean_of("product_name_match")],
    ),
    "catalog-match": ([evaluators.catalog], [evaluators.mean_of("match_correct")]),
    "reference-markets": (
        [evaluators.markets],
        [evaluators.mean_of("markets_recall"), evaluators.mean_of("must_include_hit")],
    ),
}

SCORE_CONFIGS: list[dict] = [
    {"name": name, "data_type": "NUMERIC", "min_value": 0, "max_value": 1, "description": description}
    for name, description in [
        ("product_found_correct", "Identification agreed with the case on whether a product is present."),
        ("product_name_match", "The product name matched one of the case's accepted spellings."),
        ("category_present", "A category was returned."),
        ("brand_correct", "The brand matched the case."),
        ("match_correct", "Catalog matching chose the expected entry, or correctly chose none."),
        ("markets_recall", "Share of the case's expected markets the answer named."),
        ("must_include_hit", "The markets the answer could not miss were named."),
        ("task_completion_rate", "Requested markets that scored, over markets requested."),
        ("tool_invocation_accuracy", "Collection calls that were needed and on-request, over all of them."),
        ("hallucination_rate", "Figures in the narrative with no support in the evidence, over figures stated."),
    ]
] + [
    {"name": name, "data_type": "NUMERIC", "min_value": 0, "description": description}
    for name, description in [
        ("response_latency_seconds", "Wall clock from the start of the run to its answer."),
        ("token_consumption", "Tokens the run spent, including the judge."),
        ("analysis.attempts", "How many times the run attempted collection."),
    ]
]


def load_all() -> list[cases.DatasetFile]:
    return [cases.load(name) for name in cases.DATASETS]


def upload(client: Any, dataset_files: list[cases.DatasetFile], score_configs: list[dict]) -> list[str]:
    """Create what is missing and update every case. Never deletes."""
    report: list[str] = []
    for file in dataset_files:
        try:
            client.get_dataset(file.name)
            report.append(f"{file.name}: exists")
        except NotFoundError:
            client.create_dataset(name=file.name, description=file.description)
            report.append(f"{file.name}: created")
        for case in file.items:
            client.create_dataset_item(
                dataset_name=file.name,
                id=case.id,
                input=case.input,
                expected_output=case.expected_output,
                metadata={"note": case.note} if case.note else None,
            )
        report.append(f"{file.name}: {len(file.items)} cases uploaded")
    for config in score_configs:
        try:
            client.api.score_configs.create(**config)
            report.append(f"score config {config['name']}: created")
        except Exception as exc:  # noqa: BLE001 — a config that already exists is not a failure
            report.append(f"score config {config['name']}: skipped ({type(exc).__name__})")
    return report


def estimate(dataset: str, count: int) -> float:
    return COST_PER_CASE[dataset] * count


def _run(client: Any, dataset: str, limit: int | None, label: str) -> None:
    file = cases.load(dataset)
    items = file.items if limit is None else file.items[:limit]
    item_evaluators, run_evaluators = EVALUATORS[dataset]
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
    result = client.get_dataset(dataset).run_experiment(
        name=f"{dataset} @ {label}",
        run_name=f"{dataset} @ {label} — {stamp}",
        description=f"{len(items)} cases, prompts from the {label} label",
        task=tasks.TASKS[dataset],
        evaluators=item_evaluators,
        run_evaluators=run_evaluators,
        max_concurrency=4,
        metadata={"label": label, "cases": str(len(items))},
    )
    print(result.format() if hasattr(result, "format") else result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run an eval dataset as a Langfuse experiment.")
    parser.add_argument("dataset", nargs="?", choices=list(cases.DATASETS), help="which dataset to run")
    parser.add_argument("--all", action="store_true", help="run every dataset")
    parser.add_argument("--upload", action="store_true", help="create/update datasets and score configs, then exit")
    parser.add_argument("--full", action="store_true", help=f"run every case (default: the first {SMOKE_CASES})")
    parser.add_argument("--label", default="production", help="the Langfuse prompt label to run against")
    parser.add_argument("--yes", action="store_true", help="do not ask before spending")
    args = parser.parse_args(argv)

    client = observability.client()
    if client is None:
        print("Langfuse is not configured: set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY.", file=sys.stderr)
        return 1

    if args.upload:
        for line in upload(client, load_all(), SCORE_CONFIGS):
            print(line)
        return 0

    chosen = list(cases.DATASETS) if args.all else ([args.dataset] if args.dataset else [])
    if not chosen:
        parser.error("name a dataset, or pass --all or --upload")

    limit = None if args.full else SMOKE_CASES
    plan = [
        (name, len(cases.load(name).items) if limit is None else min(limit, len(cases.load(name).items)))
        for name in chosen
    ]
    total = sum(estimate(name, count) for name, count in plan)
    for name, count in plan:
        print(f"{name}: {count} cases, about US${estimate(name, count):.2f}")
    print(f"total: about US${total:.2f}, prompts from the {args.label} label")
    if not args.yes and input("run it? [y/N] ").strip().lower() not in ("y", "yes"):
        print("nothing run.")
        return 2

    settings.langfuse_prompt_label = args.label
    asyncio.run(asyncio.to_thread(prompts.warm, PROMPTS))
    for name, _ in plan:
        _run(client, name, limit, args.label)
    return 0


if __name__ == "__main__":
    sys.exit(main())
