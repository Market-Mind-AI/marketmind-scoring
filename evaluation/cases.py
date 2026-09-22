"""The cases each prompt is measured against.

They live in evals/*.json rather than in code because they are the yardstick,
not the tool: they get read and argued with in review, and they are uploaded to
Langfuse as dataset items whose ids stay stable, so a re-upload updates a case
instead of adding a second copy of it.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DATASETS: tuple[str, ...] = ("identify-product", "catalog-match", "reference-markets")

# The markets an analysis can score (app/db seed, migration 003). A case that
# expects a country outside it would be asking for an answer the product could
# never give.
SUPPORTED_COUNTRIES: tuple[str, ...] = (
    "ZAF", "ARG", "BRA", "CAN", "CHL", "COL", "MEX", "PER", "USA", "IND", "IDN", "JPN",
    "SAU", "TUR", "FRA", "DEU", "ITA", "NLD", "POL", "PRT", "ESP", "SWE", "GBR", "AUS",
)

_DIRECTORY = Path(__file__).resolve().parents[1] / "evals"


@dataclass(frozen=True)
class Case:
    id: str
    input: dict[str, Any]
    expected_output: dict[str, Any]
    note: str | None = None


@dataclass(frozen=True)
class DatasetFile:
    name: str
    description: str
    items: list[Case]


def load(dataset: str) -> DatasetFile:
    """The cases for one dataset, as written in evals/<dataset>.json."""
    if dataset not in DATASETS:
        raise ValueError(f"unknown dataset {dataset!r}; expected one of {', '.join(DATASETS)}")
    raw = json.loads((_DIRECTORY / f"{dataset}.json").read_text(encoding="utf-8"))
    items = [
        Case(id=item["id"], input=item["input"], expected_output=item["expected_output"], note=item.get("note"))
        for item in raw["items"]
    ]
    return DatasetFile(name=raw["dataset"], description=raw["description"], items=items)
