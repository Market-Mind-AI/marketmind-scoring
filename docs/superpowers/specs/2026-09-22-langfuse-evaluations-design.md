# Evaluations on Langfuse: datasets, experiments and evaluators — design

**Date:** 2026-09-22 · **Status:** approved in chat, pending spec review
**Repo:** `marketmind-core` · **Branch:** `feat/langfuse-evaluations` (stacked on `feat/langfuse-prompt-store`)

## Context

Piece 3 of the Langfuse program: (1) LLM tracing ✅, (2) prompt store ✅, (3) evaluations, (4) long-term memory.

What exists today is **online** measurement only: every analysis reports five measures as Langfuse scores, and the narrative's figures are checked against the evidence packet. Nothing measures a prompt **offline**, against fixed cases with known answers, so "is version 2 better than version 1" can only be answered by running real analyses and reading them by hand.

## Goal

1. Three Langfuse **datasets** of cases with expected answers, versioned in git.
2. A script that runs a dataset against the live code as a Langfuse **experiment**, with deterministic evaluators, so runs are comparable in the Langfuse UI.
3. Comparing **prompt versions**: the same cases run against any Langfuse label, so v1 and v2 sit side by side.
4. **Score configs** for the five run measures and the eval metrics, so the UI shows names, types and ranges.
5. A documented **Langfuse evaluator** (LLM-as-a-judge, run by Langfuse) for the narrative claims that carry no figure.

## Decisions (from the brainstorm)

- **Datasets:** product identification, catalog matching and reference markets. The narrative is **out of scope** here: it needs evidence packets, which only completed analyses produce, and that needs live DataForSEO.
- **Judge:** Langfuse-managed evaluators, not the orchestrator's in-code judge. The orchestrator is not touched in this piece, and its `app/quality.py` judge stays off and documented as such.
- **Cases live in git** (`evals/*.json`) and are uploaded to Langfuse, the same way prompts are seeded.
- **Default verification is a 5-case smoke sweep** (at least US$0.65: 5 × (0.045 + 0.011 + 0.073)). The full sweep (about US$2.20 per prompt version) is opt-in. Every figure here is a floor, not a ceiling: adaptive thinking is on by default and billed as output, so any given case can cost more than its measured estimate.

## Non-goals

- Narrative/hallucination dataset and experiments.
- Running experiments in CI, or on a schedule.
- Promoting a prompt version automatically from a score.
- Changing what the app does at runtime. Nothing here runs inside a request.

## The datasets

| Langfuse dataset | Cases | Input | Expected output |
|---|---|---|---|
| `identify-product` | ~25 | `hint_text`, `countries` | `product_found`, `product_name` (with accepted aliases), `category`, `brand` |
| `catalog-match` | ~15 | `text`, `candidates` (title, category) | `expected_title`, or `null` for "no match" |
| `reference-markets` | ~12 | `product_name`, `brand`, `category` | `expected_markets` (ISO3 list), `must_include` (ISO3 list) |

The cases are drafted from the rules the prompts themselves state, and reviewed in the PR. They include the cases the prompts exist to get right:
- **identify:** non-products (`"quero"`, `"hello"`, a question with no product) must return `product_found: false`; a branded item keeps the brand out of `product_name`; an accessory gets its own category.
- **catalog:** look-alikes that must **not** match (pork "picanha" vs beef picanha; "yerba buena" vs yerba mate; an oil deep-fryer vs an air fryer).
- **markets:** products whose biggest markets are well known, with `must_include` as the non-negotiable part of the answer.

**File shape** (`evals/identify-product.json`, one file per dataset):

```json
{
  "dataset": "identify-product",
  "description": "Text-only identification, including inputs that name no product.",
  "items": [
    {
      "id": "airfryer-basic",
      "input": {"hint_text": "air fryer 5.5L digital", "countries": ["BRA"]},
      "expected_output": {"product_found": true, "product_name": ["air fryer"], "category_required": true, "brand": null}
    }
  ]
}
```

`id` is stable: re-uploading updates the item in place instead of duplicating it. `product_name` is a list of accepted spellings.

## Components

### `evaluation/` (new package at the repo root)

Importable and side-effect free, so it can be unit-tested without touching Langfuse.

- **`cases.py`:** `load(dataset: str) -> DatasetFile` reads and validates a file from `evals/` (unique ids, required keys, known dataset name).
- **`tasks.py`:** one task function per dataset, each calling the real client the service uses, so an experiment exercises the same code path as production:
  - `identify_product(item)` → `VisionClient().identify_product(image_base64=None, image_media_type=None, hint_text=…, countries=…)`
  - `match_catalog(item)` → `CatalogMatcher(provider=resolve_catalog_match_llm()).pick(text=…, candidates=[…])`, with candidates built as small stand-in objects carrying `entity_key`, `canonical_title`, `category` and `localized_markets` (the attributes the matcher reads), so no database is needed
  - `reference_markets(item)` → `ReferenceMarketClient().candidates(product_name=…, brand=…, category=…, supported_countries=…)`
- **`evaluators.py`:** pure functions returning `langfuse.Evaluation(name=…, value=…, comment=…)`:
  - `product_found_correct` (1/0), `product_name_match` (1/0, normalized: lowercased, accents folded, matched against the accepted list), `category_present` (1/0), `brand_correct` (1/0, only when the case states a brand)
  - `match_correct` (1/0, where "no match" is an expected answer in its own right)
  - `markets_recall` (|predicted ∩ expected| ÷ |expected|), `must_include_hit` (1/0)
  - Run-level: the mean of each metric across the run.

### `scripts/run_eval.py` (new)

```bash
python scripts/run_eval.py --upload                          # create/update datasets and score configs (free)
python scripts/run_eval.py identify-product                  # smoke: 5 cases, against the production label
python scripts/run_eval.py identify-product --full           # every case
python scripts/run_eval.py reference-markets --label candidate --full
python scripts/run_eval.py --all --full                      # all three datasets
```

- **`--upload`** is idempotent: it creates each dataset if missing (`create_dataset`), upserts every item by `id` (`create_dataset_item`), and creates the score configs. It makes no model calls and costs nothing.
- **A run** calls `get_dataset(name).run_experiment(name=…, run_name=…, task=…, evaluators=…, run_evaluators=…, max_concurrency=4)`. Langfuse records the run, each item's trace and every score.
- **`--label`** (default `production`) selects which prompt version the task functions use, via the new setting below. The run name carries the dataset, the label and the timestamp, so runs are told apart in the UI.
- **It refuses to run without Langfuse keys**, and prints the per-case and mean scores at the end.
- **It always prints the case count and an estimated cost, and asks for confirmation** unless `--yes` is given. The estimates come from measured per-call costs: about $0.045 for identification, $0.011 for catalog matching and $0.073 for reference markets.
- Before the first task it calls `prompts.warm(PROMPTS)`, because piece 2 only fetches prompts Langfuse has confirmed.

### Prompt label selection

`app/core/config.py` gains `langfuse_prompt_label: str = "production"`, and `app/prompts.py` uses it instead of the hard-coded `LABEL`. `scripts/run_eval.py --label candidate` sets it for its own process only. Nothing changes at runtime, where the default stays `production`.

To compare versions in Langfuse you add the `candidate` label to the version you want to try, run the same dataset against both labels, and compare the two runs.

### Score configs

Created by `--upload` through the Langfuse API (`score_configs.create`), so the UI shows each score with its type and range:

| Score | Type | Range | Where it comes from |
|---|---|---|---|
| `task_completion_rate`, `tool_invocation_accuracy`, `hallucination_rate` | NUMERIC | 0–1 | each analysis (orchestrator) |
| `response_latency_seconds`, `token_consumption`, `analysis.attempts` | NUMERIC | ≥ 0 | each analysis (orchestrator) |
| `product_found_correct`, `product_name_match`, `category_present`, `brand_correct`, `match_correct`, `must_include_hit` | NUMERIC | 0–1 | experiments |
| `markets_recall` | NUMERIC | 0–1 | experiments |

### The Langfuse evaluator (LLM-as-a-judge), set up in the UI

This is configuration, not code, because Langfuse calls the model itself. The spec carries the exact text so it can be reproduced:

- **Target:** generations named `explanation` (the narrative), sampled.
- **Score name:** `narrative_support`, NUMERIC 0–1.
- **Prompt:**
  > You are checking one business narrative against the evidence it was written from. Judge ONLY claims the evidence does not support. A claim is unsupported when the evidence contains nothing that would establish it — not when it is merely phrased differently, rounded, or summarised. Return 1 when every claim is supported, 0 when any is not, and list the unsupported claims.
- **Note:** it stays idle until an analysis completes and writes a narrative, which needs live DataForSEO. The PR says so.
- **Key:** Langfuse stores the LLM key you give it. Use a separate Anthropic key for it, not the one the services use.

## Costs

Nothing runs by itself. Uploading is free. Per run: about $1.15 for `identify-product` (25 cases), $0.17 for `catalog-match` (15) and $0.88 for `reference-markets` (12). The 5-case smoke sweep over all three is at least $0.65 (5 × (0.045 + 0.011 + 0.073)). The judge costs about $0.02 per narrative it judges, at whatever sampling you set in Langfuse. Every figure above is a floor: adaptive thinking is on by default and billed as output, so a case can cost more.

## Testing

- **`tests/test_evaluation_cases.py`:** every file in `evals/` parses, ids are unique, each case has the keys its dataset requires, and the counts are what the spec says.
- **`tests/test_evaluators.py`:** each evaluator against hand-written outputs, including the tricky ones — accent-folded name matches, "no match" as the right answer, partial market recall, and a `brand_correct` that is skipped when the case states no brand.
- **`tests/test_eval_tasks.py`:** each task function with a fake provider, asserting it calls the real client with the case's fields and returns the shape the evaluators expect. No network.
- **`tests/test_run_eval.py`:** the upload logic against a fake Langfuse client (datasets created once, items upserted by id, score configs created), and that a run refuses to start without keys.
- **No test makes a paid call.** The experiment itself is only ever run by hand.

## Delivery

A `feat/langfuse-evaluations` branch and PR in `marketmind-core`, stacked on `feat/langfuse-prompt-store`. The PR explains how to upload, how to run a smoke sweep, how to compare labels, and how to set up the evaluator in the UI.

## Risks

- **The cases are the yardstick.** A wrong expected answer makes a good prompt look bad. They're reviewed in the PR, and each case says in a `note` why that answer is right.
- **Model non-determinism:** two runs of the same version won't score identically. Small differences are noise; the spec sets no automatic threshold, and nothing is promoted automatically.
- **Cost by accident:** the script always states the case count and estimated cost and asks before spending, unless `--yes` is passed.
