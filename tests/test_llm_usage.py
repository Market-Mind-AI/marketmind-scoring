"""What a request spent on models, reported back to whoever asked for it."""

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.llm import usage


def test_counts_anthropic_and_openai_shapes() -> None:
    # Two providers, two names for the same two numbers.
    with usage.collect() as spend:
        usage.record("claude-opus-5", SimpleNamespace(input_tokens=1200, output_tokens=300))
        usage.record("gpt-5", SimpleNamespace(prompt_tokens=800, completion_tokens=100))

    assert spend.input_tokens == 2000
    assert spend.output_tokens == 400
    assert spend.total_tokens == 2400
    assert spend.calls == 2
    assert spend.models == ["claude-opus-5", "gpt-5"]


def test_a_call_whose_provider_reports_nothing_is_counted_as_such() -> None:
    """Not as zero. A total of zero tokens over three calls has to be readable
    as 'the provider did not say' and not as 'this was free', or the cost
    figure quietly understates itself every time a gateway omits the counts."""
    with usage.collect() as spend:
        usage.record("qwen3-vl:8b-instruct", None)
        usage.record("claude-opus-5", SimpleNamespace(input_tokens=10, output_tokens=5))

    assert spend.calls == 2
    assert spend.calls_without_usage == 1
    assert spend.total_tokens == 15


def test_recording_outside_a_tally_is_harmless() -> None:
    # A model call made by a background job, not by a request. Nothing to add
    # it to, and nothing that should break because of it.
    usage.record("claude-opus-5", SimpleNamespace(input_tokens=10, output_tokens=5))


def test_the_response_carries_what_the_request_spent() -> None:
    app = FastAPI()

    from app.main import report_llm_usage

    app.middleware("http")(report_llm_usage)

    @app.get("/thinks")
    def thinks() -> dict[str, str]:
        usage.record("claude-opus-5", SimpleNamespace(input_tokens=1200, output_tokens=300))
        return {"ok": "yes"}

    @app.get("/does-not-think")
    def does_not_think() -> dict[str, str]:
        return {"ok": "yes"}

    client = TestClient(app)

    spent = client.get("/thinks")
    assert spent.headers["X-LLM-Input-Tokens"] == "1200"
    assert spent.headers["X-LLM-Output-Tokens"] == "300"
    assert spent.headers["X-LLM-Calls"] == "1"

    # No model call, no headers — so the caller sums only what was really spent.
    free = client.get("/does-not-think")
    assert "X-LLM-Calls" not in free.headers


def test_one_request_never_sees_another_request_s_spend() -> None:
    app = FastAPI()

    from app.main import report_llm_usage

    app.middleware("http")(report_llm_usage)

    @app.get("/thinks")
    def thinks() -> dict[str, str]:
        usage.record("claude-opus-5", SimpleNamespace(input_tokens=100, output_tokens=10))
        return {"ok": "yes"}

    client = TestClient(app)
    first = client.get("/thinks")
    second = client.get("/thinks")

    assert first.headers["X-LLM-Input-Tokens"] == "100"
    assert second.headers["X-LLM-Input-Tokens"] == "100"  # not 200


def test_cached_prompt_tokens_still_count_as_input() -> None:
    """Anthropic's `input_tokens` is only the uncached remainder. Without the
    cached parts, turning caching on would read as the prompts shrinking — and
    the run's cost as whatever the cache happened to leave over."""
    with usage.collect() as spend:
        usage.record(
            "claude-opus-5",
            SimpleNamespace(
                input_tokens=200, output_tokens=300, cache_read_input_tokens=900, cache_creation_input_tokens=100
            ),
        )

    assert spend.input_tokens == 1200
    assert spend.cache_read_tokens == 900
    assert spend.cache_write_tokens == 100


def test_the_response_says_how_much_of_the_prompt_came_from_the_cache() -> None:
    app = FastAPI()

    from app.main import report_llm_usage

    app.middleware("http")(report_llm_usage)

    @app.get("/cached")
    def cached() -> dict[str, str]:
        usage.record(
            "claude-opus-5",
            SimpleNamespace(input_tokens=200, output_tokens=300, cache_read_input_tokens=1000),
        )
        return {"ok": "yes"}

    @app.get("/uncached")
    def uncached() -> dict[str, str]:
        usage.record("claude-opus-5", SimpleNamespace(input_tokens=200, output_tokens=300))
        return {"ok": "yes"}

    client = TestClient(app)
    spent = client.get("/cached")
    assert spent.headers["X-LLM-Input-Tokens"] == "1200"
    assert spent.headers["X-LLM-Cache-Read-Tokens"] == "1000"
    assert spent.headers["X-LLM-Cache-Write-Tokens"] == "0"
    assert "X-LLM-Cache-Read-Tokens" not in client.get("/uncached").headers
