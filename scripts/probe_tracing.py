"""Send one standard trace per situation the Langfuse views filter on.

Makes no model call — it costs nothing but a few requests to Langfuse.

    python scripts/probe_tracing.py                    # every situation, environment "probe"
    python scripts/probe_tracing.py --only error,slow  # a subset
    python scripts/probe_tracing.py --environment local

The Tracing page in Langfuse comes with views that filter on what a generation
carries — Quality (level ERROR or WARNING), Slow (latency), Cost (cost and
tokens). When those views are empty it is impossible to tell "nothing is wrong"
from "nothing is being reported": the same empty table. These probes answer
that. Each one is a root generation shaped exactly like the ones
TracedProvider records (app/services/llm/traced.py) — same model, same usage
keys, same level and status_message vocabulary — so what the views show for a
probe is what they will show for the real call in the same situation.

They go to their own environment ("probe" by default) and carry the tag
"probe", so they never pass for an analysis in any view that filters by either.
A probe's latency and cost are declared, not spent: the slow one ends its span
90 s after it started without waiting, and the expensive ones report the token
counts a $1+ call would, against a model Langfuse prices on its own.
"""

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.services.llm.traced import NO_USAGE_WARNING  # noqa: E402

MODEL = "claude-opus-5"
TAG = "probe"


@dataclass(frozen=True)
class Probe:
    """One situation, as the generation that reports it."""

    name: str
    view: str
    level: str = "DEFAULT"
    status_message: str | None = None
    usage: dict[str, int] | None = None
    seconds: float = 2.0
    output: Any = "ok"


PROBES: tuple[Probe, ...] = (
    Probe(
        name="probe-ok",
        view="baseline: in no view, priced, DEFAULT level",
        usage={"input": 1200, "output": 300, "input_cache_read": 900},
    ),
    Probe(
        name="probe-warning",
        view="Quality (level WARNING)",
        level="WARNING",
        status_message=NO_USAGE_WARNING,
        usage=None,
    ),
    Probe(
        name="probe-error",
        view="Quality (level ERROR)",
        level="ERROR",
        status_message="LLMError: Claude returned no parseable result (stop_reason=max_tokens)",
        usage={"input": 900, "output": 16000},
        output=None,
    ),
    Probe(
        name="probe-slow",
        view="Slow (latency 90 s)",
        usage={"input": 1200, "output": 300},
        seconds=90.0,
    ),
    Probe(
        name="probe-high-cost",
        view="Cost (> $1 on claude-opus-5: 190k in at $5/M + 10k out at $25/M = $1.20)",
        usage={"input": 190_000, "output": 10_000},
        seconds=40.0,
    ),
    Probe(
        name="probe-high-tokens",
        view="Cost (high token usage: 420k tokens)",
        usage={"input": 400_000, "output": 20_000},
        seconds=60.0,
    ),
)


def emit(client: Any, probes: tuple[Probe, ...] = PROBES, *, now: datetime | None = None) -> list[tuple[Probe, str]]:
    """Send each probe as its own trace; return (probe, trace_id) pairs."""
    from langfuse import propagate_attributes

    started = now or datetime.now(UTC)
    sent: list[tuple[Probe, str]] = []
    for probe in probes:
        with propagate_attributes(trace_name=probe.name, tags=[TAG], metadata={"probe": probe.view}):
            generation = client.start_observation(
                name=probe.name,
                as_type="generation",
                model=MODEL,
                input={"system": "You are a probe.", "user": f"Report the situation: {probe.view}"},
                metadata={"provider": "probe", "site": probe.name, "view": probe.view},
            )
            update: dict[str, Any] = {"level": probe.level}
            if probe.status_message:
                update["status_message"] = probe.status_message
            if probe.usage is not None:
                update["usage_details"] = probe.usage
            if probe.output is not None:
                update["output"] = probe.output
            generation.update(**update)
            # Nanoseconds since the epoch, as OpenTelemetry counts an end time.
            ends_at = started + timedelta(seconds=probe.seconds)
            generation.end(end_time=int(ends_at.timestamp() * 1_000_000_000))
            sent.append((probe, generation.trace_id))
    return sent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send one standard Langfuse trace per situation the views filter on.")
    parser.add_argument("--only", default="", help="comma-separated probe names (without the probe- prefix)")
    parser.add_argument("--environment", default="probe", help="the Langfuse environment the probes go to")
    args = parser.parse_args(argv)

    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        print("Langfuse is not configured: set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY.", file=sys.stderr)
        return 1
    wanted = {f"probe-{name.strip()}" for name in args.only.split(",") if name.strip()}
    probes = tuple(p for p in PROBES if not wanted or p.name in wanted)
    if not probes:
        print(f"No probe matches {sorted(wanted)}; known: {[p.name for p in PROBES]}", file=sys.stderr)
        return 2

    from langfuse import Langfuse

    client = Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        host=settings.langfuse_host,
        environment=args.environment,
    )
    sent = emit(client, probes)
    client.flush()
    for probe, trace_id in sent:
        print(f"{probe.name:18s} {probe.level:8s} {probe.view}")
        print(f"{'':18s} {client.get_trace_url(trace_id=trace_id)}")
    print(f"\n{len(sent)} probe trace(s) sent to environment '{args.environment}', tag '{TAG}'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
