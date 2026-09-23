# Langfuse tracing for core's LLM calls — design

**Date:** 2026-09-21 · **Status:** approved in chat, pending spec review
**Repos:** `marketmind-core` (main change), `marketmind-orchestrator` (header only)

## Context

This is piece 1 of 4 in the Langfuse work, and the others build on it:

1. **LLM tracing** (this spec)
2. Prompt store: Langfuse Prompt Management for core's hard-coded prompts
3. Evaluations: datasets, experiments, and the LLM judge
4. Long-term memory for the LangGraph agent

The orchestrator already traces each analysis. Since `fix/langfuse-scores`, every
run opens a root `analysis` span whose trace id is
`Langfuse.create_trace_id(seed=analysis_id)`, and the graph nodes nest under it.
Every model call happens in core, though, and core is not instrumented. The
traces therefore show which node ran but not what was asked of a model, what it
answered, or what it cost. Piece 2 needs those generations to exist, since a
prompt version is linked to the generations that used it.

## Goal

Every LLM call core makes shows up in Langfuse as a *generation*. When an
orchestrator run causes the call, the generation sits inside that run's trace.

## Non-goals

- Prompt management (piece 2), datasets and experiments (piece 3).
- Tracing core's non-LLM work (DB queries, collector calls).
- Tracing the Ollama gateway service itself. Calls to it are traced here like
  any other provider.

## Design

### Joining the orchestrator's trace: W3C `traceparent`

- **Orchestrator:** `ScoringClient._headers()` adds the OpenTelemetry context of
  the current span via `opentelemetry.propagate.inject(headers)`. This yields
  `traceparent`, plus `baggage` carrying `langfuse_trace_id`. Outside a run there
  is no current span and nothing is added.
- **Core:** the existing per-request middleware (`app/main.py`, the one that
  opens `llm_usage.collect()`) extracts the context from the request headers
  with `opentelemetry.propagate.extract` and attaches it for the request.
  Requests without the header, such as the browser's `identify-image` call before
  any analysis exists, start their own trace.

We verified on 2026-09-21 with the installed SDK that a generation opened in a
context holding only the extracted header gets the seeded trace id, with the
`analysis` span as its parent.

### Core: `app/observability.py`

This mirrors the orchestrator's module:

- Tracing is enabled only when `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY`
  are both set (`LANGFUSE_HOST` defaults to `https://cloud.langfuse.com`). Being
  unconfigured means being untraced, which is allowed, and core logs it once at
  startup.
- Tracing never raises into a model call. A tracing failure is logged at debug
  level and the call's result is returned unchanged.

### Core: `TracedProvider(inner, site)`

- This is a wrapper implementing `LLMProvider`. The resolver returns it around
  whichever provider `resolve_llm` built, so Claude, GPT and Ollama are covered
  without touching any provider.
- Each `resolve_*_llm()` passes its **site** name, which becomes the generation
  name: `vision`, `enrichment`, `reference_market`, `catalog_match`,
  `explanation`.
- A generation records:
  - **model** (`inner.model`) and **provider** (in metadata).
  - **input**: `{"system": ..., "user": ..., "image": "[image/jpeg, 2.3 MB]"}`.
    The photo is always replaced by that placeholder and never uploaded.
  - **output**: the text, or the parsed model's `model_dump()`.
  - **usage**: the input/output token delta of the request's `llm_usage` tally
    across the call. The middleware opens one tally per request, so providers
    stay unchanged. With no tally open, usage is omitted. `usage.py` gains a
    public `current() -> Usage | None` accessor for this, since today the
    context variable is private.
  - **latency**, which the span measures itself.
  - **errors**: level `ERROR` with the exception message, after which the
    exception is re-raised unchanged.
- When tracing is disabled the wrapper is not applied at all, so there is zero
  overhead.

### Config

- Core's `.env` and `.env.example` get `LANGFUSE_PUBLIC_KEY`,
  `LANGFUSE_SECRET_KEY` and `LANGFUSE_HOST`, pointing at the same Langfuse project
  as the orchestrator.
- The `langfuse` dependency is added to core's `pyproject.toml` (Poetry), matching
  the orchestrator's `^4.15`.

## Testing

**Core unit tests (`tests/test_llm_tracing.py`):**
- The generation is named after the site and carries model, input and output.
- The image is replaced by the placeholder, and no base64 reaches the recorded
  input.
- Token usage comes from the tally delta.
- A provider error marks the generation as `ERROR` and re-raises the original
  exception.
- Without keys, the resolver returns the bare provider.
- A request carrying `traceparent` produces a generation in that trace.

**Orchestrator unit test:** inside `observability.run(id)`,
`ScoringClient._headers()` contains a `traceparent` whose trace id is the seeded
one. Outside a run, it contains no `traceparent`.

**Live check:** run one analysis. Its trace in Langfuse shows core's
`enrichment` generation (and the other sites the run reaches) under `analysis`,
with tokens and the prompt text. A standalone `identify-image` call shows up as
its own trace with a `vision` generation and the photo placeholder.

## Delivery

A `feat/langfuse-llm-tracing` branch and PR in each repo, for Álvaro to review.
The orchestrator branch sits on top of `fix/langfuse-scores`.

## Risks

- **Payload size.** Explanation prompts carry the evidence packet (a few KB),
  which is fine. Images are excluded by design.
- **Personal data.** User-typed `user_prompt` text reaches Langfuse Cloud (EU).
  This is accepted in the payload decision (text yes, photo no).
