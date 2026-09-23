# Evaluations on Langfuse: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Three Langfuse datasets of cases with known answers, deterministic evaluators, and a script that runs a dataset against the live code as a Langfuse experiment, so prompt versions can be compared.

**Architecture:** Cases live in `evals/*.json` and are uploaded to Langfuse. An importable `evaluation/` package holds the loader, the task functions (which call the real clients) and the evaluators (pure functions). `scripts/run_eval.py` ties them together and gates spending.

**Tech stack:** Python 3.12+, Langfuse SDK v4 (`create_dataset`, `create_dataset_item`, `get_dataset(...).run_experiment`, `Evaluation`, `score_configs`), pytest (`asyncio_mode = "auto"`).

**Spec:** `docs/superpowers/specs/2026-09-22-langfuse-evaluations-design.md`

## Global constraints

- Dataset names, exactly: `identify-product`, `catalog-match`, `reference-markets`.
- Score names, exactly: `product_found_correct`, `product_name_match`, `category_present`, `brand_correct`, `match_correct`, `markets_recall`, `must_include_hit`.
- Every evaluator is deterministic: no model call, no network.
- **No paid API calls without the user's explicit approval.** Tests use fakes. `--upload` and every test are free. Only an experiment run spends, and the script must state the case count and estimated cost and ask, unless `--yes` is given.
- Per-call cost estimates used for that message: identification $0.045, catalog matching $0.011, reference markets $0.073.
- The runtime default label stays `production`; `--label` only changes the script's own process.
- Branch `feat/langfuse-evaluations` in `marketmind-core` (stacked on `feat/langfuse-prompt-store`). Never commit to `main`.
- Commit messages: a plain imperative sentence, a blank line, then `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- Never print `.env` values. Never commit `poetry.lock`.

## File map

| File | Responsibility |
|---|---|
| `evals/identify-product.json`, `evals/catalog-match.json`, `evals/reference-markets.json` (new) | the cases, reviewed in the PR |
| `evaluation/__init__.py`, `evaluation/cases.py` (new) | load and validate a case file |
| `evaluation/evaluators.py` (new) | deterministic scoring, returning `Evaluation` |
| `evaluation/tasks.py` (new) | one task function per dataset, calling the real clients |
| `scripts/run_eval.py` (new) | upload, score configs, run an experiment, cost gate |
| `app/core/config.py`, `app/prompts.py` | `langfuse_prompt_label` setting, used instead of the hard-coded label |
| `tests/test_evaluation_cases.py`, `tests/test_evaluators.py`, `tests/test_eval_tasks.py`, `tests/test_run_eval.py` (new) | tests, all free |

## How to run core's tests

```bash
cd /Users/miguel/Projects/market-mind-org/marketmind-core
DATABASE_HOST=localhost DATABASE_PORT=5436 DATABASE_NAME=marketmind_test poetry run pytest <path> -v
```

The suite is **520 passed** before this plan.

---

### Task 1: The cases and their loader

**Files:**
- Create: `evals/identify-product.json`, `evals/catalog-match.json`, `evals/reference-markets.json`, `evaluation/__init__.py`, `evaluation/cases.py`, `tests/test_evaluation_cases.py`

**Interfaces:**
- Produces:
  - `evaluation.cases.load(dataset: str) -> DatasetFile`, where `DatasetFile` is a dataclass with `.name: str`, `.description: str`, `.items: list[Case]`
  - `Case` is a dataclass with `.id: str`, `.input: dict`, `.expected_output: dict`, `.note: str | None`
  - `evaluation.cases.DATASETS: tuple[str, ...] = ("identify-product", "catalog-match", "reference-markets")`

- [ ] **Step 1: Write the case files**

`evals/identify-product.json` — `input` is `{hint_text, countries}`; `expected_output` is `{product_found, product_name (accepted spellings), category_required, brand}`:

```json
{
  "dataset": "identify-product",
  "description": "Text-only identification. Includes inputs that name no product, brands that must stay out of product_name, and accessories that get their own category.",
  "items": [
    {"id": "air-fryer", "input": {"hint_text": "air fryer 5.5L digital", "countries": ["BRA"]}, "expected_output": {"product_found": true, "product_name": ["air fryer"], "category_required": true, "brand": null}},
    {"id": "manuka-honey", "input": {"hint_text": "Miel de manuka UMF 10+, frasco de 250 g", "countries": ["COL"]}, "expected_output": {"product_found": true, "product_name": ["manuka honey"], "category_required": true, "brand": null}},
    {"id": "olive-oil", "input": {"hint_text": "Aceite de oliva virgen extra, botella de 1 litro", "countries": ["ESP"]}, "expected_output": {"product_found": true, "product_name": ["extra virgin olive oil", "olive oil"], "category_required": true, "brand": null}},
    {"id": "colombian-coffee", "input": {"hint_text": "Café colombiano de especialidad en grano, 500 g", "countries": ["MEX"]}, "expected_output": {"product_found": true, "product_name": ["colombian coffee", "coffee beans", "coffee"], "category_required": true, "brand": null}},
    {"id": "picanha", "input": {"hint_text": "picanha bovina resfriada 1 kg", "countries": ["BRA"]}, "expected_output": {"product_found": true, "product_name": ["picanha", "beef picanha"], "category_required": true, "brand": null}},
    {"id": "branded-whisky", "input": {"hint_text": "Cardhu 12 años whisky single malt 700 ml", "countries": ["GBR"]}, "expected_output": {"product_found": true, "product_name": ["single malt whisky", "whisky", "scotch whisky"], "category_required": true, "brand": "Cardhu"}, "note": "The brand belongs in brand; product_name stays generic."},
    {"id": "branded-capsules", "input": {"hint_text": "Nespresso Vertuo capsules, 40 pack", "countries": ["FRA"]}, "expected_output": {"product_found": true, "product_name": ["coffee capsules", "coffee pods", "coffee capsule"], "category_required": true, "brand": "Nespresso"}},
    {"id": "non-latin-script", "input": {"hint_text": "カーデュ 12年 ウイスキー", "countries": ["JPN"]}, "expected_output": {"product_found": true, "product_name": ["single malt whisky", "whisky", "scotch whisky"], "category_required": true, "brand": "Cardhu"}, "note": "Japanese input; the brand is still Cardhu and the name still generic."},
    {"id": "accessory-liners", "input": {"hint_text": "papel para airfryer, 100 hojas", "countries": ["BRA"]}, "expected_output": {"product_found": true, "product_name": ["air fryer liners", "air fryer paper liners", "parchment liners"], "category_required": true, "brand": null}, "note": "An accessory gets its own category, never the category of the appliance."},
    {"id": "spare-part-filter", "input": {"hint_text": "filtro HEPA para aspiradora robot", "countries": ["ESP"]}, "expected_output": {"product_found": true, "product_name": ["hepa filter", "vacuum filter"], "category_required": true, "brand": null}},
    {"id": "cookbook", "input": {"hint_text": "libro de recetas de cocina italiana", "countries": ["ITA"]}, "expected_output": {"product_found": true, "product_name": ["cookbook", "recipe book"], "category_required": true, "brand": null}},
    {"id": "yerba-mate", "input": {"hint_text": "yerba mate suave 1 kg", "countries": ["ARG"]}, "expected_output": {"product_found": true, "product_name": ["yerba mate", "mate"], "category_required": true, "brand": null}},
    {"id": "headphones", "input": {"hint_text": "audífonos bluetooth con cancelación de ruido", "countries": ["MEX"]}, "expected_output": {"product_found": true, "product_name": ["bluetooth headphones", "wireless headphones", "headphones", "noise cancelling headphones"], "category_required": true, "brand": null}},
    {"id": "robot-vacuum", "input": {"hint_text": "robot aspirador con mapeo láser", "countries": ["ESP"]}, "expected_output": {"product_found": true, "product_name": ["robot vacuum", "robot vacuum cleaner", "robotic vacuum"], "category_required": true, "brand": null}},
    {"id": "creatine", "input": {"hint_text": "creatina monohidratada 300 g", "countries": ["BRA"]}, "expected_output": {"product_found": true, "product_name": ["creatine monohydrate", "creatine"], "category_required": true, "brand": null}},
    {"id": "sunscreen", "input": {"hint_text": "protector solar facial SPF 50", "countries": ["COL"]}, "expected_output": {"product_found": true, "product_name": ["sunscreen", "facial sunscreen", "sunblock", "sun cream"], "category_required": true, "brand": null}},
    {"id": "dog-food", "input": {"hint_text": "alimento seco para perros adultos 15 kg", "countries": ["CHL"]}, "expected_output": {"product_found": true, "product_name": ["dry dog food", "dog food"], "category_required": true, "brand": null}},
    {"id": "diapers", "input": {"hint_text": "pañales talla 3, 100 unidades", "countries": ["PER"]}, "expected_output": {"product_found": true, "product_name": ["diapers", "nappies", "baby diapers"], "category_required": true, "brand": null}},
    {"id": "drill", "input": {"hint_text": "taladro percutor inalámbrico 20V", "countries": ["MEX"]}, "expected_output": {"product_found": true, "product_name": ["cordless hammer drill", "hammer drill", "cordless drill", "drill"], "category_required": true, "brand": null}},
    {"id": "office-chair", "input": {"hint_text": "silla ergonómica de oficina con soporte lumbar", "countries": ["USA"]}, "expected_output": {"product_found": true, "product_name": ["ergonomic office chair", "office chair", "desk chair"], "category_required": true, "brand": null}},
    {"id": "running-shoes", "input": {"hint_text": "zapatillas de running para mujer talla 38", "countries": ["ESP"]}, "expected_output": {"product_found": true, "product_name": ["running shoes", "women's running shoes", "womens running shoes"], "category_required": true, "brand": null}},
    {"id": "green-tea", "input": {"hint_text": "té verde matcha en polvo 100 g", "countries": ["JPN"]}, "expected_output": {"product_found": true, "product_name": ["matcha", "matcha powder", "matcha green tea", "green tea"], "category_required": true, "brand": null}},
    {"id": "no-product-quero", "input": {"hint_text": "quero", "countries": ["BRA"]}, "expected_output": {"product_found": false, "product_name": [], "category_required": false, "brand": null}, "note": "Names no product: must not be turned into one."},
    {"id": "no-product-hello", "input": {"hint_text": "hello", "countries": ["USA"]}, "expected_output": {"product_found": false, "product_name": [], "category_required": false, "brand": null}},
    {"id": "no-product-question", "input": {"hint_text": "¿cuál es el mejor país para vender?", "countries": ["COL"]}, "expected_output": {"product_found": false, "product_name": [], "category_required": false, "brand": null}, "note": "A question with no product in it."}
  ]
}
```

`evals/catalog-match.json` — `input` is `{text, candidates: [{title, category}]}`; `expected_output` is `{expected_title}` where `null` means "no candidate is the same product":

```json
{
  "dataset": "catalog-match",
  "description": "The LLM step that decides whether a query names the same product as a catalog entry. Half the cases must answer 'none'.",
  "items": [
    {"id": "alias-air-fryer", "input": {"text": "freidora de aire", "candidates": [{"title": "air fryer", "category": "Kitchen Appliances"}, {"title": "deep fryer", "category": "Kitchen Appliances"}]}, "expected_output": {"expected_title": "air fryer"}},
    {"id": "oil-deep-fryer", "input": {"text": "fritadeira de óleo", "candidates": [{"title": "air fryer", "category": "Kitchen Appliances"}, {"title": "deep fryer", "category": "Kitchen Appliances"}]}, "expected_output": {"expected_title": "deep fryer"}, "note": "An oil deep-fryer is not an air fryer."},
    {"id": "pork-picanha", "input": {"text": "picanha suína", "candidates": [{"title": "picanha", "category": "Beef"}, {"title": "pork shoulder", "category": "Pork"}]}, "expected_output": {"expected_title": null}, "note": "Pork picanha is a different product from beef picanha, and no candidate is it."},
    {"id": "yerba-buena", "input": {"text": "yerba buena", "candidates": [{"title": "yerba mate", "category": "Tea & Infusions"}, {"title": "mint", "category": "Herbs"}]}, "expected_output": {"expected_title": "mint"}},
    {"id": "coconut-oil", "input": {"text": "aceite de coco orgánico", "candidates": [{"title": "extra virgin olive oil", "category": "Cooking Oils"}, {"title": "coconut oil", "category": "Cooking Oils"}]}, "expected_output": {"expected_title": "coconut oil"}},
    {"id": "brand-partition", "input": {"text": "Cardhu 12", "candidates": [{"title": "single malt whisky", "category": "Spirits"}, {"title": "Cardhu single malt whisky", "category": "Spirits"}]}, "expected_output": {"expected_title": "Cardhu single malt whisky"}, "note": "A query naming a brand resolves to that brand's entry, not the generic family."},
    {"id": "generic-not-brand", "input": {"text": "whisky single malt", "candidates": [{"title": "Cardhu single malt whisky", "category": "Spirits"}]}, "expected_output": {"expected_title": null}, "note": "The generic term must not resolve to one brand's entry."},
    {"id": "translation", "input": {"text": "café colombiano en grano", "candidates": [{"title": "colombian coffee", "category": "Coffee"}]}, "expected_output": {"expected_title": "colombian coffee"}},
    {"id": "japanese-script", "input": {"text": "カーデュ 12年", "candidates": [{"title": "Cardhu single malt whisky", "category": "Spirits"}, {"title": "green tea", "category": "Tea"}]}, "expected_output": {"expected_title": "Cardhu single malt whisky"}},
    {"id": "accessory-vs-appliance", "input": {"text": "papel para airfryer", "candidates": [{"title": "air fryer", "category": "Kitchen Appliances"}, {"title": "air fryer liners", "category": "Kitchen Accessories"}]}, "expected_output": {"expected_title": "air fryer liners"}},
    {"id": "size-variant", "input": {"text": "air fryer 8L", "candidates": [{"title": "air fryer", "category": "Kitchen Appliances"}]}, "expected_output": {"expected_title": "air fryer"}, "note": "A size variant is the same product."},
    {"id": "homonym-apple", "input": {"text": "manzana fresca", "candidates": [{"title": "Apple iPhone", "category": "Smartphones"}, {"title": "apple juice", "category": "Beverages"}]}, "expected_output": {"expected_title": null}},
    {"id": "spare-part", "input": {"text": "filtro HEPA para robot aspirador", "candidates": [{"title": "robot vacuum", "category": "Home Appliances"}, {"title": "HEPA filter", "category": "Appliance Parts"}]}, "expected_output": {"expected_title": "HEPA filter"}},
    {"id": "sunscreen-vs-aftersun", "input": {"text": "protector solar SPF 50", "candidates": [{"title": "after sun lotion", "category": "Skincare"}, {"title": "sunscreen", "category": "Skincare"}]}, "expected_output": {"expected_title": "sunscreen"}},
    {"id": "unrelated-supplements", "input": {"text": "creatina monohidratada", "candidates": [{"title": "whey protein", "category": "Supplements"}, {"title": "BCAA", "category": "Supplements"}]}, "expected_output": {"expected_title": null}}
  ]
}
```

`evals/reference-markets.json` — `input` is `{product_name, brand, category}`; `expected_output` is `{expected_markets, must_include}`, both ISO3 from the supported basket:

```json
{
  "dataset": "reference-markets",
  "description": "The biggest real markets for a product, chosen from the supported basket. expected_markets drives recall; must_include is the part the answer cannot miss.",
  "items": [
    {"id": "air-fryer", "input": {"product_name": "air fryer", "brand": null, "category": "Kitchen Appliances"}, "expected_output": {"expected_markets": ["USA", "BRA", "GBR", "DEU", "FRA"], "must_include": ["USA"]}},
    {"id": "picanha", "input": {"product_name": "picanha", "brand": null, "category": "Beef"}, "expected_output": {"expected_markets": ["BRA", "ARG", "USA"], "must_include": ["BRA"]}},
    {"id": "yerba-mate", "input": {"product_name": "yerba mate", "brand": null, "category": "Tea & Infusions"}, "expected_output": {"expected_markets": ["ARG", "BRA", "CHL", "PER"], "must_include": ["ARG"]}},
    {"id": "manuka-honey", "input": {"product_name": "manuka honey", "brand": null, "category": "Honey"}, "expected_output": {"expected_markets": ["USA", "GBR", "JPN", "AUS", "DEU"], "must_include": ["USA"]}},
    {"id": "olive-oil", "input": {"product_name": "extra virgin olive oil", "brand": null, "category": "Cooking Oils"}, "expected_output": {"expected_markets": ["ESP", "ITA", "USA", "FRA", "PRT", "DEU"], "must_include": ["ESP"]}},
    {"id": "running-shoes", "input": {"product_name": "running shoes", "brand": null, "category": "Footwear"}, "expected_output": {"expected_markets": ["USA", "DEU", "GBR", "FRA", "JPN", "BRA"], "must_include": ["USA"]}},
    {"id": "robot-vacuum", "input": {"product_name": "robot vacuum", "brand": null, "category": "Home Appliances"}, "expected_output": {"expected_markets": ["USA", "DEU", "JPN", "FRA", "GBR"], "must_include": ["USA"]}},
    {"id": "diapers", "input": {"product_name": "diapers", "brand": null, "category": "Baby Care"}, "expected_output": {"expected_markets": ["USA", "IND", "IDN", "BRA", "MEX"], "must_include": ["USA"]}},
    {"id": "dog-food", "input": {"product_name": "dry dog food", "brand": null, "category": "Pet Food"}, "expected_output": {"expected_markets": ["USA", "BRA", "DEU", "GBR", "FRA", "MEX"], "must_include": ["USA"]}},
    {"id": "matcha", "input": {"product_name": "matcha green tea", "brand": null, "category": "Tea"}, "expected_output": {"expected_markets": ["JPN", "USA", "DEU", "GBR", "FRA"], "must_include": ["JPN"]}},
    {"id": "tequila", "input": {"product_name": "tequila", "brand": null, "category": "Spirits"}, "expected_output": {"expected_markets": ["USA", "MEX", "ESP", "DEU"], "must_include": ["USA"]}},
    {"id": "sunscreen", "input": {"product_name": "sunscreen", "brand": null, "category": "Skincare"}, "expected_output": {"expected_markets": ["USA", "BRA", "AUS", "JPN", "DEU"], "must_include": ["USA"]}}
  ]
}
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_evaluation_cases.py`:

```python
"""The cases are the yardstick: they have to parse, be unique, and say what they expect."""

import pytest

from evaluation import cases


def test_every_dataset_loads() -> None:
    assert cases.DATASETS == ("identify-product", "catalog-match", "reference-markets")
    for name in cases.DATASETS:
        loaded = cases.load(name)
        assert loaded.name == name
        assert loaded.description
        assert loaded.items


def test_the_case_counts_are_what_the_spec_says() -> None:
    assert len(cases.load("identify-product").items) == 25
    assert len(cases.load("catalog-match").items) == 15
    assert len(cases.load("reference-markets").items) == 12


@pytest.mark.parametrize("name", ["identify-product", "catalog-match", "reference-markets"])
def test_ids_are_unique(name: str) -> None:
    ids = [case.id for case in cases.load(name).items]

    assert len(ids) == len(set(ids))


def test_identification_cases_carry_what_the_evaluators_read() -> None:
    for case in cases.load("identify-product").items:
        assert case.input["hint_text"]
        assert case.input["countries"]
        assert isinstance(case.expected_output["product_found"], bool)
        assert isinstance(case.expected_output["product_name"], list)
        assert isinstance(case.expected_output["category_required"], bool)


def test_a_quarter_of_the_identification_cases_name_no_product() -> None:
    """The rule they exist to check: text that names no product must not become one."""
    found = [c for c in cases.load("identify-product").items if not c.expected_output["product_found"]]

    assert len(found) >= 3


def test_catalog_cases_include_look_alikes_that_must_not_match() -> None:
    items = cases.load("catalog-match").items
    for case in items:
        assert case.input["text"]
        assert case.input["candidates"]
        assert set(case.expected_output) == {"expected_title"}
    assert sum(1 for c in items if c.expected_output["expected_title"] is None) >= 4


def test_market_cases_expect_only_supported_countries() -> None:
    basket = set(cases.SUPPORTED_COUNTRIES)
    for case in cases.load("reference-markets").items:
        expected = case.expected_output["expected_markets"]
        must = case.expected_output["must_include"]
        assert set(expected) <= basket, case.id
        assert set(must) <= set(expected), case.id


def test_an_unknown_dataset_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown dataset"):
        cases.load("does-not-exist")
```

- [ ] **Step 3: Run it to confirm it fails**

Run: `DATABASE_HOST=localhost DATABASE_PORT=5436 DATABASE_NAME=marketmind_test poetry run pytest tests/test_evaluation_cases.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'evaluation'`).

- [ ] **Step 4: Implement the loader**

Create `evaluation/__init__.py` (empty) and `evaluation/cases.py`:

```python
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
```

- [ ] **Step 5: Run the test to confirm it passes**

Run: `... poetry run pytest tests/test_evaluation_cases.py -v`
Expected: 9 passed. If a count assertion fails, fix the case file, not the test: the counts are 25 / 15 / 12.

- [ ] **Step 6: Commit**

```bash
git add evals evaluation tests/test_evaluation_cases.py
git commit -m "Write the cases each prompt is measured against

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: The evaluators

**Files:**
- Create: `evaluation/evaluators.py`, `tests/test_evaluators.py`

**Interfaces:**
- Consumes: `Case` (Task 1), `langfuse.Evaluation`.
- Produces, each taking `(*, input: dict, output: dict | None, expected_output: dict, **kwargs) -> list[Evaluation]`:
  - `identification(...)` → `product_found_correct`, and when a product is expected, `product_name_match`, `category_present`, and `brand_correct` only if the case states a brand
  - `catalog(...)` → `match_correct`
  - `markets(...)` → `markets_recall`, `must_include_hit`
  - `mean_of(name)` → a run-level evaluator returning the mean of that metric
  - `normalize(text: str) -> str`, lowercased, accent-folded, punctuation-stripped, single-spaced

- [ ] **Step 1: Write the failing tests**

Create `tests/test_evaluators.py`:

```python
"""Scoring is deterministic: same output, same score, no model in the loop."""

from evaluation import evaluators


def _by_name(results) -> dict[str, float]:
    return {e.name: e.value for e in results}


def test_a_correct_identification_scores_every_metric() -> None:
    scores = _by_name(
        evaluators.identification(
            input={"hint_text": "air fryer 5.5L"},
            output={"product_found": True, "product_name": "Air Fryer", "category": "Kitchen Appliances", "brand": None},
            expected_output={"product_found": True, "product_name": ["air fryer"], "category_required": True, "brand": None},
        )
    )

    assert scores == {"product_found_correct": 1.0, "product_name_match": 1.0, "category_present": 1.0}


def test_accents_and_case_do_not_change_a_name_match() -> None:
    scores = _by_name(
        evaluators.identification(
            input={},
            output={"product_found": True, "product_name": "Café Colombiano", "category": "Coffee", "brand": None},
            expected_output={"product_found": True, "product_name": ["cafe colombiano"], "category_required": True, "brand": None},
        )
    )

    assert scores["product_name_match"] == 1.0


def test_a_name_outside_the_accepted_list_does_not_match() -> None:
    scores = _by_name(
        evaluators.identification(
            input={},
            output={"product_found": True, "product_name": "kitchen appliance", "category": "Kitchen", "brand": None},
            expected_output={"product_found": True, "product_name": ["air fryer"], "category_required": True, "brand": None},
        )
    )

    assert scores["product_name_match"] == 0.0


def test_brand_is_scored_only_when_the_case_states_one() -> None:
    with_brand = _by_name(
        evaluators.identification(
            input={},
            output={"product_found": True, "product_name": "whisky", "category": "Spirits", "brand": "cardhu"},
            expected_output={"product_found": True, "product_name": ["whisky"], "category_required": True, "brand": "Cardhu"},
        )
    )
    without_brand = _by_name(
        evaluators.identification(
            input={},
            output={"product_found": True, "product_name": "whisky", "category": "Spirits", "brand": "Something"},
            expected_output={"product_found": True, "product_name": ["whisky"], "category_required": True, "brand": None},
        )
    )

    assert with_brand["brand_correct"] == 1.0
    assert "brand_correct" not in without_brand


def test_inventing_a_product_where_there_is_none_scores_zero() -> None:
    scores = _by_name(
        evaluators.identification(
            input={"hint_text": "quero"},
            output={"product_found": True, "product_name": "air fryer", "category": "Kitchen", "brand": None},
            expected_output={"product_found": False, "product_name": [], "category_required": False, "brand": None},
        )
    )

    assert scores == {"product_found_correct": 0.0}


def test_correctly_refusing_to_invent_a_product_scores_one() -> None:
    scores = _by_name(
        evaluators.identification(
            input={"hint_text": "quero"},
            output={"product_found": False},
            expected_output={"product_found": False, "product_name": [], "category_required": False, "brand": None},
        )
    )

    assert scores == {"product_found_correct": 1.0}


def test_a_failed_call_scores_zero_rather_than_crashing() -> None:
    scores = _by_name(
        evaluators.identification(
            input={},
            output=None,
            expected_output={"product_found": True, "product_name": ["air fryer"], "category_required": True, "brand": None},
        )
    )

    assert scores["product_found_correct"] == 0.0


def test_the_right_catalog_entry_scores_one() -> None:
    scores = _by_name(
        evaluators.catalog(
            input={}, output={"matched_title": "air fryer"}, expected_output={"expected_title": "air fryer"}
        )
    )

    assert scores == {"match_correct": 1.0}


def test_no_match_is_an_answer_in_its_own_right() -> None:
    right = _by_name(
        evaluators.catalog(input={}, output={"matched_title": None}, expected_output={"expected_title": None})
    )
    wrong = _by_name(
        evaluators.catalog(input={}, output={"matched_title": "picanha"}, expected_output={"expected_title": None})
    )

    assert right == {"match_correct": 1.0}
    assert wrong == {"match_correct": 0.0}


def test_market_recall_is_the_share_of_expected_markets_named() -> None:
    scores = _by_name(
        evaluators.markets(
            input={},
            output={"markets": ["USA", "BRA", "CHN"]},
            expected_output={"expected_markets": ["USA", "BRA", "GBR", "DEU"], "must_include": ["USA"]},
        )
    )

    assert scores["markets_recall"] == 0.5
    assert scores["must_include_hit"] == 1.0


def test_missing_a_must_include_market_is_caught_even_with_good_recall() -> None:
    scores = _by_name(
        evaluators.markets(
            input={},
            output={"markets": ["ARG", "CHL", "PER"]},
            expected_output={"expected_markets": ["ARG", "CHL", "PER", "BRA"], "must_include": ["BRA"]},
        )
    )

    assert scores["markets_recall"] == 0.75
    assert scores["must_include_hit"] == 0.0


def test_the_run_level_evaluator_averages_one_metric() -> None:
    class _Result:
        def __init__(self, evaluations):
            self.evaluations = evaluations

    from langfuse import Evaluation

    item_results = [
        _Result([Evaluation(name="match_correct", value=1.0)]),
        _Result([Evaluation(name="match_correct", value=0.0)]),
        _Result([Evaluation(name="other", value=9.0)]),
    ]

    mean = evaluators.mean_of("match_correct")(item_results=item_results)

    assert [(e.name, e.value) for e in mean] == [("match_correct_mean", 0.5)]
```

- [ ] **Step 2: Run them to confirm they fail**

Run: `... poetry run pytest tests/test_evaluators.py -v`
Expected: FAIL (`ImportError: cannot import name 'evaluators'`).

- [ ] **Step 3: Implement**

Create `evaluation/evaluators.py`:

```python
"""What counts as a right answer, decided without a model.

Every metric here is exact: the same output always scores the same, and a
disagreement is about the case, not about the judge. The model-graded half of
evaluation lives in Langfuse (the narrative-support evaluator), where the thing
being judged is prose rather than a value with a known answer.
"""

import unicodedata
from typing import Any

from langfuse import Evaluation


def normalize(text: str) -> str:
    """Compare names the way a person would: case, accents and punctuation aside."""
    folded = unicodedata.normalize("NFKD", (text or "").strip().lower())
    stripped = "".join(c for c in folded if not unicodedata.combining(c))
    kept = "".join(c if c.isalnum() or c.isspace() else " " for c in stripped)
    return " ".join(kept.split())


def identification(*, input: dict, output: dict | None, expected_output: dict, **kwargs: Any) -> list[Evaluation]:
    found = bool((output or {}).get("product_found"))
    expected_found = bool(expected_output["product_found"])
    results = [Evaluation(name="product_found_correct", value=1.0 if found == expected_found else 0.0)]
    if not expected_found or not found:
        # Nothing else is meaningful: either the case expects no product, or the
        # run produced none, and scoring an absent name as wrong twice would
        # count the same mistake twice.
        return results

    accepted = {normalize(name) for name in expected_output["product_name"]}
    results.append(
        Evaluation(
            name="product_name_match",
            value=1.0 if normalize((output or {}).get("product_name", "")) in accepted else 0.0,
            comment=f"got {(output or {}).get('product_name')!r}",
        )
    )
    if expected_output.get("category_required"):
        results.append(Evaluation(name="category_present", value=1.0 if (output or {}).get("category") else 0.0))
    expected_brand = expected_output.get("brand")
    if expected_brand:
        results.append(
            Evaluation(
                name="brand_correct",
                value=1.0 if normalize((output or {}).get("brand") or "") == normalize(expected_brand) else 0.0,
                comment=f"got {(output or {}).get('brand')!r}",
            )
        )
    return results


def catalog(*, input: dict, output: dict | None, expected_output: dict, **kwargs: Any) -> list[Evaluation]:
    matched = (output or {}).get("matched_title")
    expected = expected_output["expected_title"]
    correct = (matched is None and expected is None) or (
        matched is not None and expected is not None and normalize(matched) == normalize(expected)
    )
    return [Evaluation(name="match_correct", value=1.0 if correct else 0.0, comment=f"got {matched!r}")]


def markets(*, input: dict, output: dict | None, expected_output: dict, **kwargs: Any) -> list[Evaluation]:
    named = {m.upper() for m in (output or {}).get("markets", [])}
    expected = {m.upper() for m in expected_output["expected_markets"]}
    must = {m.upper() for m in expected_output["must_include"]}
    recall = len(named & expected) / len(expected) if expected else 0.0
    return [
        Evaluation(name="markets_recall", value=recall, comment=f"named {sorted(named)}"),
        Evaluation(name="must_include_hit", value=1.0 if must <= named else 0.0, comment=f"missing {sorted(must - named)}"),
    ]


def mean_of(metric: str):
    """A run-level evaluator: one number for the whole run, per metric."""

    def run_evaluator(*, item_results: list[Any], **kwargs: Any) -> list[Evaluation]:
        values = [
            evaluation.value
            for result in item_results
            for evaluation in getattr(result, "evaluations", [])
            if evaluation.name == metric and isinstance(evaluation.value, (int, float))
        ]
        if not values:
            return []
        return [Evaluation(name=f"{metric}_mean", value=sum(values) / len(values))]

    return run_evaluator
```

- [ ] **Step 4: Run the tests to confirm they pass**

Run: `... poetry run pytest tests/test_evaluators.py -v`
Expected: 12 passed.

- [ ] **Step 5: Commit**

```bash
git add evaluation/evaluators.py tests/test_evaluators.py
git commit -m "Score the eval cases without a model in the loop

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: The task functions

**Files:**
- Create: `evaluation/tasks.py`, `tests/test_eval_tasks.py`

**Interfaces:**
- Consumes: `VisionClient`, `CatalogMatcher`, `ReferenceMarketClient`, `resolve_catalog_match_llm`, `cases.SUPPORTED_COUNTRIES`.
- Produces, each `async def f(*, item, **kwargs) -> dict`, where `item` is a Langfuse dataset item or a dict with `.input` / `["input"]`:
  - `identify_product` → `{"product_found": bool, "product_name": str | None, "category": str | None, "brand": str | None}`
  - `match_catalog` → `{"matched_title": str | None}`
  - `reference_markets` → `{"markets": [ISO3, ...]}`
  - `TASKS: dict[str, Callable]` keyed by dataset name
  - `input_of(item) -> dict`, accepting both shapes

- [ ] **Step 1: Write the failing tests**

Create `tests/test_eval_tasks.py`:

```python
"""Each task runs the real client, so an experiment measures the code that ships."""

from unittest.mock import AsyncMock, patch

from app.services.reference_market_client import ReferenceCandidatesResult
from app.services.vision_client import MarketTerms, ProductIdentification
from evaluation import tasks


def _identification(**overrides) -> ProductIdentification:
    fields = {
        "product_found": True,
        "product_name": "air fryer",
        "brand": None,
        "category": "Kitchen Appliances",
        "target_age_min": None,
        "target_age_max": None,
        "estimated_price": None,
        "price_currency": None,
        "confidence": 0.9,
    }
    fields.update(overrides)
    return ProductIdentification(**fields)


async def test_identification_passes_the_case_to_the_real_client() -> None:
    provider = AsyncMock()
    provider.complete = AsyncMock(return_value=_identification())

    with patch("app.services.vision_client.resolve_vision_llm", return_value=provider):
        result = await tasks.identify_product(
            item={"input": {"hint_text": "air fryer 5.5L", "countries": ["BRA"]}}
        )

    assert result == {
        "product_found": True,
        "product_name": "air fryer",
        "category": "Kitchen Appliances",
        "brand": None,
    }
    kwargs = provider.complete.await_args.kwargs
    assert "air fryer 5.5L" in kwargs["user"]
    assert kwargs["image"] is None


async def test_a_refused_identification_is_reported_not_raised() -> None:
    """The client raises when the model says no product; for a case that expects
    exactly that, the refusal IS the answer."""
    provider = AsyncMock()
    provider.complete = AsyncMock(return_value=_identification(product_found=False, product_name=""))

    with patch("app.services.vision_client.resolve_vision_llm", return_value=provider):
        result = await tasks.identify_product(item={"input": {"hint_text": "quero", "countries": ["BRA"]}})

    assert result == {"product_found": False, "product_name": None, "category": None, "brand": None}


async def test_catalog_matching_builds_candidates_from_the_case() -> None:
    """The matcher maps the model's entity_key back to a candidate, so the fake
    echoes the first key the prompt showed it — the same thing a model does."""
    import re

    class _EchoFirstCandidate:
        def __init__(self) -> None:
            self.user: str | None = None

        async def complete(self, *, system=None, user, image=None, response_model=None):
            from app.services.catalog_matcher import CatalogMatchDecision

            self.user = user
            key = re.search(r"entity_key: (\S+)", user).group(1)
            return CatalogMatchDecision(entity_key=key, reasoning="same product")

    provider = _EchoFirstCandidate()

    with patch("evaluation.tasks._catalog_provider", return_value=provider):
        result = await tasks.match_catalog(
            item={
                "input": {
                    "text": "freidora de aire",
                    "candidates": [{"title": "air fryer", "category": "Kitchen Appliances"}],
                }
            }
        )

    assert result == {"matched_title": "air fryer"}
    assert "air fryer" in provider.user
    assert "freidora de aire" in provider.user


async def test_catalog_matching_reports_no_match_as_none() -> None:
    from app.services.catalog_matcher import CatalogMatchDecision

    provider = AsyncMock()
    provider.complete = AsyncMock(return_value=CatalogMatchDecision(entity_key="", reasoning="none of them"))

    with patch("evaluation.tasks._catalog_provider", return_value=provider):
        result = await tasks.match_catalog(
            item={"input": {"text": "manzana", "candidates": [{"title": "Apple iPhone", "category": "Smartphones"}]}}
        )

    assert result == {"matched_title": None}


async def test_reference_markets_asks_for_the_supported_basket() -> None:
    provider = AsyncMock()
    provider.complete = AsyncMock(
        return_value=ReferenceCandidatesResult(
            candidates=[
                MarketTerms(country="USA", primary_name="air fryer", search_terms=["air fryer"], confidence=0.9),
                MarketTerms(country="BRA", primary_name="fritadeira", search_terms=["fritadeira"], confidence=0.9),
            ]
        )
    )

    with patch("app.services.reference_market_client.resolve_reference_market_llm", return_value=provider):
        result = await tasks.reference_markets(
            item={"input": {"product_name": "air fryer", "brand": None, "category": "Kitchen Appliances"}}
        )

    assert result == {"markets": ["USA", "BRA"]}
    assert "USA" in provider.complete.await_args.kwargs["user"]


def test_every_dataset_has_a_task() -> None:
    from evaluation import cases

    assert set(tasks.TASKS) == set(cases.DATASETS)


def test_input_is_read_from_either_item_shape() -> None:
    class _Item:
        input = {"hint_text": "x"}

    assert tasks.input_of({"input": {"hint_text": "x"}}) == {"hint_text": "x"}
    assert tasks.input_of(_Item()) == {"hint_text": "x"}
```

- [ ] **Step 2: Run them to confirm they fail**

Run: `... poetry run pytest tests/test_eval_tasks.py -v`
Expected: FAIL (`ImportError: cannot import name 'tasks'`).

- [ ] **Step 3: Implement**

Create `evaluation/tasks.py`:

```python
"""What an experiment actually runs: the same clients a request runs.

A task calls the production client, so an experiment measures the prompt as it
is used — including the resolver, the prompt store and the traced provider —
rather than a copy of it written for the test.
"""

import uuid
from typing import Any, Callable

from app.services.catalog_matcher import CatalogMatcher
from app.services.llm.resolver import resolve_catalog_match_llm
from app.services.reference_market_client import ReferenceMarketClient
from app.services.vision_client import ProductNotIdentifiedError, VisionClient
from evaluation.cases import SUPPORTED_COUNTRIES


def input_of(item: Any) -> dict:
    """The case's input, whether it arrives as a Langfuse item or a plain dict."""
    if isinstance(item, dict):
        return item["input"]
    return item.input


def _catalog_provider():
    """Indirection so a test can supply a provider without a database."""
    return resolve_catalog_match_llm()


class _Candidate:
    """What CatalogMatcher reads off a catalog row. The rows themselves live in
    the database; a case names its candidates, so the experiment needs none."""

    def __init__(self, title: str, category: str | None) -> None:
        self.entity_key = uuid.uuid4()
        self.canonical_title = title
        self.category = category
        self.localized_markets = None


async def identify_product(*, item: Any, **kwargs: Any) -> dict:
    case = input_of(item)
    try:
        result = await VisionClient().identify_product(
            image_base64=None,
            image_media_type=None,
            hint_text=case["hint_text"],
            countries=case["countries"],
        )
    except ProductNotIdentifiedError:
        # The client refuses to return a product the model did not find. For a
        # case that expects exactly that, the refusal is the answer.
        return {"product_found": False, "product_name": None, "category": None, "brand": None}
    return {
        "product_found": True,
        "product_name": result.product_name,
        "category": result.category,
        "brand": result.brand,
    }


async def match_catalog(*, item: Any, **kwargs: Any) -> dict:
    case = input_of(item)
    candidates = [_Candidate(c["title"], c.get("category")) for c in case["candidates"]]
    chosen = await CatalogMatcher(provider=_catalog_provider()).pick(text=case["text"], candidates=candidates)
    return {"matched_title": chosen.canonical_title if chosen is not None else None}


async def reference_markets(*, item: Any, **kwargs: Any) -> dict:
    case = input_of(item)
    found = await ReferenceMarketClient().candidates(
        product_name=case["product_name"],
        brand=case.get("brand"),
        category=case.get("category"),
        supported_countries=list(SUPPORTED_COUNTRIES),
    )
    return {"markets": [market.country for market in found]}


TASKS: dict[str, Callable] = {
    "identify-product": identify_product,
    "catalog-match": match_catalog,
    "reference-markets": reference_markets,
}
```

- [ ] **Step 4: Run the tests to confirm they pass**

Run: `... poetry run pytest tests/test_eval_tasks.py -v`
Expected: 7 passed.

If `ProductNotIdentifiedError` is not exported from `app.services.vision_client`, import it from where it is defined and say so in your report.

- [ ] **Step 5: Commit**

```bash
git add evaluation/tasks.py tests/test_eval_tasks.py
git commit -m "Run each eval case through the client the service uses

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: The run script and the prompt-label setting

**Files:**
- Create: `scripts/run_eval.py`, `tests/test_run_eval.py`
- Modify: `app/core/config.py` (next to the other `langfuse_*` settings), `app/prompts.py` (use the setting instead of the constant)

**Interfaces:**
- Consumes: `cases.load`, `cases.DATASETS`, `tasks.TASKS`, `evaluators.*`, `observability.client()`, `prompts.warm`, `app.prompt_registry.PROMPTS`.
- Produces:
  - `scripts/run_eval.py`: `upload(client, dataset_files, score_configs) -> list[str]`, `estimate(dataset: str, count: int) -> float`, `main(argv=None) -> int`
  - `COST_PER_CASE: dict[str, float]`, `SCORE_CONFIGS: list[dict]`
  - `app.prompts.LABEL` stays for compatibility; `prompt_text` and `warm` read `settings.langfuse_prompt_label`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_run_eval.py`:

```python
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
        def __init__(self, outer):
            self.score_configs = outer

    def create_score_config(self, **config) -> None:
        self.score_configs.append(config)


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
```

Also append to `tests/test_prompts.py`:

```python
def test_the_label_comes_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """So an experiment can score a candidate version without touching runtime,
    where the label stays production."""
    from app.core.config import settings

    stored = _stored("enrichment-system", "candidate text")
    fake = FakeLangfuse(result=stored)
    monkeypatch.setattr(observability, "client", lambda: fake)
    monkeypatch.setattr(settings, "langfuse_prompt_label", "candidate")
    prompts._known.add("enrichment-system")

    prompts.prompt_text("enrichment-system", "code text")

    assert fake.requests[0][1]["label"] == "candidate"
```

- [ ] **Step 2: Run them to confirm they fail**

Run: `... poetry run pytest tests/test_run_eval.py tests/test_prompts.py -v`
Expected: FAIL — `run_eval` does not exist, and the label test fails because the label is a constant.

- [ ] **Step 3: Add the setting and use it**

In `app/core/config.py`, directly after `langfuse_host`:

```python
    # Which Langfuse label the prompts are fetched from. Runtime stays on
    # "production"; scripts/run_eval.py points a single run at another label to
    # score a candidate version against the same cases.
    langfuse_prompt_label: str = "production"
```

In `app/prompts.py`, replace uses of the module constant `LABEL` inside `prompt_text` and `warm` with `settings.langfuse_prompt_label`, importing `from app.core.config import settings`. Keep `LABEL = "production"` defined, since `scripts/sync_prompts.py` imports it.

- [ ] **Step 4: Implement the script**

Create `scripts/run_eval.py`:

```python
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
from datetime import datetime, timezone
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
            client.create_score_config(**config)
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
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
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
    plan = [(name, len(cases.load(name).items) if limit is None else min(limit, len(cases.load(name).items))) for name in chosen]
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
```

- [ ] **Step 5: Run the tests**

Run: `... poetry run pytest tests/test_run_eval.py tests/test_prompts.py tests/test_evaluation_cases.py -v`
Expected: all pass.

If the SDK's score-config call is not `client.create_score_config(...)`, find the right one (look for `score_configs` under the installed `langfuse/api`), use it, adjust the fake in the test to match, and say so in your report.

- [ ] **Step 6: Run the full suite and ruff**

Run the full suite; report the count. Then `poetry run ruff check app evaluation scripts tests`.

- [ ] **Step 7: Commit**

```bash
git add scripts/run_eval.py tests/test_run_eval.py tests/test_prompts.py app/core/config.py app/prompts.py
git commit -m "Run a dataset as a Langfuse experiment, against any prompt label

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Upload the datasets and score configs (free: no model calls)

- [ ] **Step 1: Rebuild core so the container has the new code**

```bash
cd /Users/miguel/Projects/market-mind-org/marketmind-core
docker compose up -d --build marketmind-scoring-api
until curl -sf localhost:8004/health >/dev/null; do sleep 2; done
```

- [ ] **Step 2: Upload (no model calls)**

```bash
docker exec marketmind-scoring-api python scripts/run_eval.py --upload
```

Expected: three datasets created, 52 cases uploaded, score configs created. Run it again: the datasets now say `exists` and the case count is the same, proving items update rather than duplicate.

- [ ] **Step 3: Confirm through the API (free)**

```bash
cd /Users/miguel/Projects/market-mind-org/marketmind-orchestrator && poetry run python - <<'EOF'
import base64, json, urllib.request
from app.config import get_settings
s = get_settings()
auth = {"Authorization": "Basic " + base64.b64encode(f"{s.langfuse_public_key}:{s.langfuse_secret_key}".encode()).decode()}
base = s.langfuse_host.rstrip("/")
get = lambda p: json.load(urllib.request.urlopen(urllib.request.Request(base + p, headers=auth)))
for d in get("/api/public/v2/datasets?limit=50")["data"]:
    items = get(f"/api/public/dataset-items?datasetName={d['name']}&limit=1")
    print(d["name"], "| items:", items.get("meta", {}).get("totalItems"))
print("score configs:", len(get("/api/public/score-configs?limit=100")["data"]))
EOF
```

Expected: `identify-product` 25, `catalog-match` 15, `reference-markets` 12, and 13 score configs.

---

### Task 6: The smoke sweep. **Paid, so ask the user first.**

- [ ] **Step 1: Ask, stating the cost**

> "The smoke sweep runs the first 5 cases of each dataset against the production prompts: about US$0.40 in Claude Opus 5 calls. It produces three experiment runs in Langfuse with their scores. OK to run?"

- [ ] **Step 2: Run it**

```bash
docker exec marketmind-scoring-api python scripts/run_eval.py --all --yes
```

- [ ] **Step 3: Read the runs back (free)** and report each dataset's mean scores and any case that failed, with what it answered.

---

### Task 7: Push and open the PR

- [ ] `git push -u origin feat/langfuse-evaluations`
- [ ] Open a PR with base `feat/langfuse-prompt-store` (stacked on core PR #2), to be retargeted to `main` as the stack merges. The body covers: the three datasets and why those cases, the deterministic evaluators, how to upload (`--upload`, free), how to run a smoke sweep and a full sweep with their costs, how to compare a `candidate` label against `production`, the `narrative-support` evaluator already configured in the Langfuse UI and that it idles until an analysis writes a narrative, and the test counts. End with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`, and no model names in the text.
