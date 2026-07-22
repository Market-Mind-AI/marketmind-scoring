"""The DB-parametrized explanation content (migration 016) is seeded from
explanation_seed.py. These guard its completeness — every scored/context
metric has a meaning and a phrase for every band — so the final LLM never
gets a hole in the packet."""

from app.domain.enums import IndicatorCode, IndicatorDimension
from app.services.mos.explanation_seed import (
    INDICATOR_BANDS,
    MOS_BANDS,
    band_phrase_rows,
    metric_rows,
)


def test_every_indicator_and_dimension_and_mos_has_a_metric_row() -> None:
    codes = {row["metric_code"] for row in metric_rows()}
    for indicator in IndicatorCode:
        assert indicator.value in codes, f"missing metric row for indicator {indicator.value}"
    for dimension in IndicatorDimension:
        assert dimension.value in codes, f"missing metric row for dimension {dimension.value}"
    assert "mos" in codes


def test_every_metric_row_has_a_nonempty_meaning_and_label() -> None:
    for row in metric_rows():
        assert row["meaning"], f"empty meaning for {row['metric_code']}"
        assert row["label"], f"empty label for {row['metric_code']}"
        assert row["metric_kind"] in {"indicator", "dimension", "mos"}


def test_every_metric_has_a_phrase_for_every_band() -> None:
    phrases: dict[str, dict[str, str]] = {}
    for row in band_phrase_rows():
        phrases.setdefault(row["metric_code"], {})[row["band"]] = row["phrase"]

    for row in metric_rows():
        code = row["metric_code"]
        expected = MOS_BANDS if row["metric_kind"] == "mos" else INDICATOR_BANDS
        assert code in phrases, f"no band phrases for {code}"
        for band in expected:
            assert phrases[code].get(band), f"missing/empty phrase for {code} / {band}"


def test_band_ordinals_are_monotonic_per_metric() -> None:
    by_code: dict[str, list[tuple[int, str]]] = {}
    for row in band_phrase_rows():
        by_code.setdefault(row["metric_code"], []).append((row["ordinal"], row["band"]))
    for code, pairs in by_code.items():
        ordinals = sorted(o for o, _ in pairs)
        assert ordinals == list(range(len(pairs))), f"non-contiguous ordinals for {code}"
