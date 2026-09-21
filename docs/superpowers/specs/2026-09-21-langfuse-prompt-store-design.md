# Prompt store on Langfuse Prompt Management: design

**Date:** 2026-09-21 · **Status:** approved in chat, pending spec review
**Repo:** `marketmind-core` · **Branch:** `feat/langfuse-prompt-store` (stacked on `feat/langfuse-llm-tracing`)

## Context

This is piece 2 of the Langfuse program: (1) LLM tracing ✅, (2) prompt store, (3) evaluations, (4) long-term memory.

Core's model instructions are hard-coded Python constants. After piece 1, every call is a Langfuse generation. Nothing records *which version* of an instruction produced it, and changing an instruction means a code change and a deploy.

## Goal

1. The six instruction texts live in Langfuse Prompt Management and are edited there. The in-code text is only the fallback.
2. At runtime core uses the version labelled `production`. A new `production` version takes effect on the next call, within the cache TTL and without a deploy.
3. Every traced generation that used a fetched prompt is linked to that prompt name and version in Langfuse.

## Decisions (from the brainstorm)

- **Source of truth:** Langfuse, with the code text as fallback (not "code only", not "Langfuse only").
- **Explanation granularity:** the two base templates only. Structure variants and conditional rules stay in code.
- **Linking mechanism:** a context variable, the same idiom `app/services/llm/usage.py` uses for the token tally. `LLMProvider.complete` does not change.

## Non-goals

- The data-formatting parts of the user messages (product lines, country lists, catalog rows, the evidence packet) stay in code.
- Model choice, temperature, and similar settings stay in env. Langfuse prompt `config` is not used.
- Chat-type prompts and prompt composability.
- Datasets and experiments (piece 3).

## The six prompts

| Langfuse name | Variables | Default constant (stays in its file) | Used by |
|---|---|---|---|
| `catalog-match-system` | none | `_SYSTEM` in `app/services/catalog_matcher.py` | system prompt of `catalog_match` |
| `enrichment-system` | none | `_SYSTEM` in `app/services/enrichment_client.py` | system prompt of `enrichment` |
| `reference-market-system` | none | `_SYSTEM` in `app/services/reference_market_client.py` | system prompt of `reference_market` |
| `vision-instructions` | `subject`, `country_list` | new `_INSTRUCTIONS` in `app/services/vision_client.py` (today an f-string) | the instruction part of the `vision` user message. The user's label and hint are still appended in code |
| `explanation-system-pt` | `structure`, `scope_intro`, `country_rule`, `ruler_scope` | `_SYSTEM_PROMPT_PT` in `app/services/explanation_client.py` | base of the Portuguese narrative system prompt |
| `explanation-system-en` | same | `_SYSTEM_PROMPT_EN` | base of the English (and other-language) system prompt |

**Template syntax:** templates use Langfuse's `{{variable}}` syntax, both in Langfuse and in the in-code defaults. The explanation constants change from `{structure}` to `{{structure}}` (and likewise for the other three). The vision f-string becomes a `{{subject}}` / `{{country_list}}` template.

**Filling in variables:** everything goes through the SDK's own single-pass `TemplateParser.compile_template`, the function `TextPromptClient.compile` uses. We verified on 2026-09-21 that the explanation's structure fragments contain no placeholders of their own, so single-pass compilation produces exactly the text today's `.replace` chain produces. The rendered text is unchanged whenever the defaults are in use, and `tests/test_explanation_prompt.py` keeps passing.

## Components

### `app/prompts.py` (new)

```python
def prompt_text(name: str, default: str, **variables: str) -> str
def take_linked_prompt() -> Any | None   # the fetched prompt for the next generation; read-and-clear
```

`prompt_text` behaves as follows:

- **When `observability.client()` is `None`** (unconfigured, or in tests, since `tests/conftest.py` blanks the keys): it returns `compile_template(default, variables)` and links nothing.
- **Otherwise** it calls:
  ```python
  client.get_prompt(
      name, label="production", type="text", fallback=default,
      max_retries=1, fetch_timeout_seconds=3,
  )
  ```
  - Caching uses the SDK default of 60 s with background refresh, so only a process's first fetch of a prompt can block, and for at most about 3 s.
  - If the result is not a fallback (`is_fallback` is false), the call records it in a context variable for the next generation. A fallback is never linked, since Langfuse can't link one.
  - It returns `result.compile(**variables)`.
- **It never raises.** Any exception logs at debug level (`marketmind-scoring.prompts`) and returns the compiled default.

`take_linked_prompt()` returns the recorded prompt and clears it, so one fetch links at most one generation.

**Invariant:** each call site fetches its prompt immediately before its model call, in the same task. A fetch followed by a call that never happens leaves a recorded prompt behind, but the next call site's fetch overwrites it before its own call.

### `TracedProvider` (from piece 1)

`_start` passes `prompt=prompts.take_linked_prompt()` to `client.start_observation(...)` when it isn't `None`. This happens inside the existing guard, so linking can never fail a call.

### Call sites

Each replaces its constant with `prompt_text(<name>, <constant>, **vars)`:

- `catalog_matcher`, `enrichment_client`, `reference_market_client`: `system=prompt_text("…-system", _SYSTEM)`.
- `vision_client`: `instructions = prompt_text("vision-instructions", _INSTRUCTIONS, subject=subject, country_list=country_list)`.
- `explanation_client._system_prompt`: the base comes from `prompt_text("explanation-system-pt" | "-en", _SYSTEM_PROMPT_PT | _EN, structure=…, scope_intro=…, country_rule=…, ruler_scope=…)`. The rest of the assembly (language line, rules, user-question block) is unchanged.

### `scripts/sync_prompts.py` (new, makes no LLM calls)

It seeds and syncs Langfuse from the code defaults. The registry of names and default constants lives in the script, which imports the constants, so `app/prompts.py` never imports the service modules and there's no import cycle.

- **Default mode:** for each prompt, if Langfuse has no `production` version, it runs `create_prompt(name, prompt=default, labels=["production"], type="text", commit_message="Seeded from code")`. Otherwise it leaves the prompt alone. It prints one line per prompt: created, or exists with its version.
- **`--update`:** where the `production` text differs from the code default, it creates a new version labelled `production` (`commit_message="Synced from code"`). Older versions stay in Langfuse, and any UI edit that is overwritten can be re-promoted from there.
- It needs the Langfuse keys, from settings or env, and exits non-zero without them.
- Its logic lives in `sync(client, registry, update: bool) -> list[str]`, so it can be tested with a fake client.

## Error handling

- Langfuse down, slow, missing a prompt, or misconfigured: the fetch falls back to the code text after at most about 3 s on a cold cache, and to the cached version when warm. Model calls are never blocked beyond that and never fail.
- A prompt in Langfuse missing one of the variables: `compile` leaves an unfilled `{{variable}}` in place. This is a content error by whoever edited the prompt, and Langfuse's UI shows each prompt's variables. It is not guarded against in code (YAGNI).

## Testing

**`tests/test_prompts.py`:**
- Without a client, the default compiles with its variables and nothing is linked.
- A fetched `production` prompt replaces the default and is linked.
- A fallback result is used but not linked.
- An exception from `get_prompt` returns the default.
- `take_linked_prompt` is read-once.
- The rendered explanation, vision and system texts are byte-identical to today's output while the defaults are in use.

**`tests/test_llm_tracing.py`:** a generation opened after a linked fetch carries `langfuse.observation.prompt.name` and `.version`. This uses a locally constructed `TextPromptClient`, with no network.

**`tests/test_sync_prompts.py`:** with a fake client:
- seeding creates only the missing prompts
- `--update` creates a version only where the text differs
- existing prompts are untouched in default mode

**Existing suites:** they pass unchanged, including `test_explanation_prompt.py`.

**Live check, free:** run `scripts/sync_prompts.py` against the project, then confirm the six prompts exist with label `production`, through the API or UI.

**Live check, paid, so ask first:** one analysis. Its `enrichment` / `reference_market` generations show the linked prompt name and version. Optionally, edit `reference-market-system` in the UI, promote it to `production`, run again, and see version 2 linked.

## Delivery

A `feat/langfuse-prompt-store` branch and PR in core, stacked on `feat/langfuse-llm-tracing` (PR #1), for Álvaro to review.

## Risks

- **Prompt drift:** UI edits and the code defaults diverge over time. `sync_prompts.py --update` realigns Langfuse to code, and the PR description tells the team that Langfuse `production` is authoritative.
- **Cold-start latency:** up to about 3 s once per prompt per process when Langfuse is slow. This is bounded by `fetch_timeout_seconds`.
