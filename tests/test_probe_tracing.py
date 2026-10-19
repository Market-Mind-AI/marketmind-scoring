"""The probe traces are shaped like the real generations, one per situation the
Langfuse views filter on, and never pass for an analysis."""

import importlib.util
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from langfuse import Langfuse
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

_spec = importlib.util.spec_from_file_location(
    "probe_tracing", Path(__file__).resolve().parents[1] / "scripts" / "probe_tracing.py"
)
probe_tracing = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe_tracing)
PROBES, emit = probe_tracing.PROBES, probe_tracing.emit


@pytest.fixture
def exported():
    exporter = InMemorySpanExporter()
    client = Langfuse(
        public_key=f"pk-test-{uuid.uuid4()}", secret_key="sk-test", host="http://127.0.0.1:9", span_exporter=exporter
    )

    def run(probes=PROBES):
        sent = emit(client, probes, now=datetime.now(UTC))
        client.flush()
        return sent, {s.name: s for s in exporter.get_finished_spans()}

    return run


def _attr(span, key: str):
    return span.attributes.get(f"langfuse.observation.{key}")


def test_every_probe_is_a_root_generation_in_its_own_trace(exported) -> None:
    sent, spans = exported()

    assert {p.name for p, _ in sent} == set(spans)
    assert len({trace_id for _, trace_id in sent}) == len(PROBES), "one trace per probe"
    for span in spans.values():
        assert span.parent is None
        assert _attr(span, "type") == "generation"
        assert _attr(span, "model.name") == probe_tracing.MODEL


def test_the_probes_cover_quality_slow_and_cost(exported) -> None:
    _, spans = exported()

    assert _attr(spans["probe-error"], "level") == "ERROR"
    assert _attr(spans["probe-warning"], "level") == "WARNING"
    assert _attr(spans["probe-warning"], "status_message") == probe_tracing.NO_USAGE_WARNING
    assert _attr(spans["probe-ok"], "level") == "DEFAULT"

    slow = spans["probe-slow"]
    assert (slow.end_time - slow.start_time) / 1e9 >= 89, "declared, not waited for"

    cost = json.loads(_attr(spans["probe-high-cost"], "usage_details"))
    # claude-opus-5 at $5/M in and $25/M out: comfortably over the $1 bar
    assert cost["input"] * 5e-6 + cost["output"] * 25e-6 > 1.0
    tokens = json.loads(_attr(spans["probe-high-tokens"], "usage_details"))
    assert tokens["input"] + tokens["output"] > 400_000


def test_the_probes_use_the_same_usage_keys_as_the_real_generations(exported) -> None:
    _, spans = exported()

    assert set(json.loads(_attr(spans["probe-ok"], "usage_details"))) == {"input", "output", "input_cache_read"}
    assert _attr(spans["probe-warning"], "usage_details") is None, "no usage is how an unpriced call looks"


def test_every_probe_is_tagged_so_it_never_passes_for_an_analysis(exported) -> None:
    _, spans = exported()

    for name, span in spans.items():
        assert list(span.attributes.get("langfuse.trace.tags")) == [probe_tracing.TAG], name
        assert span.attributes.get("langfuse.trace.name") == name


def test_a_subset_can_be_sent(exported) -> None:
    sent, spans = exported(tuple(p for p in PROBES if p.name in {"probe-error"}))

    assert [p.name for p, _ in sent] == ["probe-error"]
    assert set(spans) == {"probe-error"}
