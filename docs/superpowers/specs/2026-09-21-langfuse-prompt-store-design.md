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

`prompt_text` is built around a **known set**: a module-level `_known: set[str]` of names Langfuse has confirmed it holds. The SDK never caches a fallback, and its HTTP client retries 5xx/timeouts on its own regardless of `max_retries` — so without this, a cold or failed fetch pays a synchronous 0.4-7 s call on the event loop on *every* request, not just the first. `prompt_text` behaves as follows:

- **When `observability.client()` is `None`**, or **when `name` is not in `_known`**: it returns `compile_template(default, variables)` and links nothing. Neither branch makes a network call — a name Langfuse hasn't confirmed is never fetched inline, cold or down.
- **Otherwise** (a known name, with a client) it calls:
  ```python
  client.get_prompt(
      name, label="production", type="text", fallback=default,
      max_retries=0, fetch_timeout_seconds=3,
  )
  ```
  For a known name this is a cache hit, or a stale hit the SDK refreshes in the background — never a cold fetch, since only `warm()` (see below) ever adds a name to `_known`, and it always fetches once before doing so.
  - If the result is a fallback, or the call raises, the name is dropped from `_known` (so the *next* call for it takes the no-network branch above, until `warm()` reconfirms it) and the link is cleared.
  - If the result's variables don't match the code default's (F3, the placeholder-drift guard): it is not used either — the compiled default is returned, unlinked, and a warning is logged once per `(name, version)`. Unlike a fallback/exception, the name stays in `_known`, so a corrected version is picked up on the next refresh.
  - Otherwise it records the result in a context variable for the next generation and returns `result.compile(**variables)`.
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

### `app/prompt_registry.py` (new) and the startup warm-up

`PROMPTS: dict[str, str]` maps each of the six names to its default constant, imported from the service modules. It lives in its own module so that `app/prompts.py` never imports the service modules, which avoids an import cycle. The sync script and the warm-up both use it.

`get_prompt` is a synchronous HTTP call, and core is async, so a cold or failed fetch inside a request would block the event loop for as long as Langfuse takes to answer — with no `max_retries` backstop, since the SDK's HTTP client retries on its own. To keep this off the request path entirely:
- `prompts.warm(registry)` calls `get_prompt(name, label="production", type="text", max_retries=0, fetch_timeout_seconds=3)` **without a fallback** — so a failure raises — for each name not already in `_known`, and adds a name to `_known` on a non-fallback success. Names already known are skipped, so steady state makes no request at all.
- When tracing is enabled, the lifespan runs it in a keep-warm loop: `asyncio.to_thread(prompts.warm, registry)` then `asyncio.sleep(60)`, forever, keeping a reference to the task like the existing sweep task.
- Startup is never delayed. A request for a name not yet confirmed just uses the code text, with no network of its own.
- A name seeded or recovered after startup is picked up by the next loop iteration, within about 60 s, without a deploy.
- Once every registry name is known, the loop's `warm()` calls are a no-op — no `get_prompt` at all — until a name is dropped from `_known` by a fallback or an exception on the request path.

**Trade-off:** a prompt edited in Langfuse (an existing, already-known name) is picked up within the SDK's own cache TTL, same as before. A name that is newly seeded, or that recovers after Langfuse was down or misconfigured, is picked up within about 60 s (the keep-warm loop's interval), rather than on the very next call.

### `scripts/sync_prompts.py` (new, makes no LLM calls)

It seeds and syncs Langfuse from the code defaults in `app/prompt_registry.PROMPTS`. It follows the existing scripts' pattern of adding the repo root to `sys.path`.

- **Default mode:** for each prompt, if Langfuse has no `production` version, it runs `create_prompt(name, prompt=default, labels=["production"], type="text", commit_message="Seeded from code")`. Otherwise it leaves the prompt alone. It prints one line per prompt: created, or exists with its version.
- **`--update`:** where the `production` text differs from the code default, it creates a new version labelled `production` (`commit_message="Synced from code"`). Older versions stay in Langfuse, and any UI edit that is overwritten can be re-promoted from there.
- It needs the Langfuse keys, from settings or env, and exits non-zero without them.
- Its logic lives in `sync(client, registry, update: bool) -> list[str]`, so it can be tested with a fake client.

## Error handling

- Langfuse down, slow, missing a prompt, or misconfigured: an unconfirmed name never fetches on the request path at all, so nothing blocks on it. A confirmed name that starts failing is dropped from `_known` on the first failure — so it, too, falls back to the code text with no further network on the request path — and is retried by the keep-warm loop.
- A prompt in Langfuse whose declared variables don't match the code default's: the placeholder-drift guard (F3) rejects it rather than let `compile` leave an unfilled `{{variable}}` in place — the compiled default is used instead, and a warning is logged once per `(name, version)`. The name stays in `_known`, so a corrected version is picked up on the next refresh.

## Testing

**`tests/test_prompts.py`:**
- Without a client, the default compiles with its variables and nothing is linked.
- An unknown (not-yet-`_known`) name returns the default without calling `get_prompt`, even with a client configured.
- After `warm()`, a known name is fetched inline with `max_retries=0`, and the result is used and linked.
- A fallback result, or an exception from `get_prompt`, drops the name from `_known` — the next call for it makes no request — and is used/returns the default, unlinked.
- `warm()` adds only non-fallback successes, skips names already known, survives exceptions, and returns the loaded count.
- `take_linked_prompt` is read-once.
- A stored prompt whose variables don't match the code default's (F3) is rejected: the default is used, unlinked, and the warning logs once per `(name, version)`.
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

**Seeding a new Langfuse project:** `_known` starts empty in every process, and a name only ever enters it once Langfuse confirms it (`warm`, i.e. a successful, non-fallback `get_prompt`). Pointing a deployment at a Langfuse project that has never been seeded means every one of the six names stays unconfirmed forever — the code text is used throughout, with no request ever failing or blocking, but also no prompt ever coming from Langfuse. Run `scripts/sync_prompts.py` against the new project *before* switching a deployment to it (see Testing's free live check). Until then, the code text is what ships.

## Risks

- **Prompt drift:** UI edits and the code defaults diverge over time. `sync_prompts.py --update` realigns Langfuse to code, and the PR description tells the team that Langfuse `production` is authoritative.
- **Placeholder drift:** an edit in the Langfuse UI that adds/removes a `{{variable}}` the code default doesn't (or does) expect is rejected by the guard in F3 rather than silently leaving a placeholder unfilled — it logs a warning and keeps using the code text until the prompt is corrected.
- **Cold or failed fetches no longer cost anything on the request path.** A name Langfuse hasn't confirmed (never seeded, or dropped after a failure) is never fetched inline — see `prompt_text`'s known-set design above. The trade-off is latency of *pickup*, not request latency: an edit to an already-known prompt lands within the SDK's cache TTL, same as before; a newly seeded or recovered name lands within about 60 s, the keep-warm loop's interval, rather than on the very next call.
