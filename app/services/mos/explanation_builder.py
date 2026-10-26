"""Deterministic builder for the per-score explanation phrase that gets stored
in indicator_values.explanation / mos_results.explanation and handed to the
final LLM (see explanation_client.py). It combines the metric's DB-parametrized
label + band phrase (from metric_explanations / metric_band_phrases, loaded via
MetricExplanationRepository) with the concrete score/band and an optional
concrete detail (e.g. the absolute search volume for Demand Volume).

No LLM here — the phrase is a fixed template over editable DB content, same
principle as the rest of the pipeline ("el LLM explica, no calcula ni altera",
Cap. 4 §4.0). The LLM only narrates these; it never produces the numbers."""

from dataclasses import dataclass


@dataclass(frozen=True)
class MetricEntry:
    """One metric's editable explanation content, as loaded from the DB."""

    kind: str  # indicator | dimension | mos
    label: str
    meaning: str
    higher_is_better: bool | None
    band_phrases: dict[str, str]  # band label -> phrase


@dataclass(frozen=True)
class ExplanationCatalog:
    """All metric explanation content (indicators + dimensions + mos), keyed by
    metric_code. Built once per request from the DB (the editable source)."""

    metrics: dict[str, MetricEntry]

    def entry(self, code: str) -> MetricEntry | None:
        return self.metrics.get(code)

    def label(self, code: str) -> str | None:
        entry = self.metrics.get(code)
        return entry.label if entry else None

    def meaning(self, code: str) -> str | None:
        entry = self.metrics.get(code)
        return entry.meaning if entry else None

    def render(
        self,
        *,
        code: str,
        score: float | None,
        band: str | None,
        detail: str | None = None,
        band_label: str | None = None,
    ) -> str | None:
        """The stored per-score sentence: "<label> - <band> (<score>/100): <band
        phrase>. <detail>". Returns None if the metric is unknown (nothing to
        narrate) — callers keep the score even when its explanation is absent.

        `band` is the English band, which is the key of the phrase;
        `band_label` is what is shown in its place, in the reader's language."""
        entry = self.metrics.get(code)
        if entry is None:
            return None
        if score is None or band is None:
            return f"{entry.label}: score unavailable."
        base = f"{entry.label} - {band_label or band} ({score:.0f}/100)"
        phrase = entry.band_phrases.get(band)
        if phrase:
            base += f": {phrase}"
        if not base.endswith("."):
            base += "."
        if detail:
            base += f" {detail.strip().rstrip('.')}."
        return base
