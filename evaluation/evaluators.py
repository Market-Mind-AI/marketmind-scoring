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
    without_quotes = stripped.replace("'", "").replace("’", "")
    kept = "".join(c if c.isalnum() or c.isspace() else " " for c in without_quotes)
    return " ".join(kept.split())


def identification(*, input: dict, output: dict | None, expected_output: dict, **kwargs: Any) -> list[Evaluation]:
    found = bool((output or {}).get("product_found"))
    expected_found = bool(expected_output["product_found"])
    results = [Evaluation(name="product_found_correct", value=1.0 if found == expected_found else 0.0)]
    if not expected_found:
        # The case expects no product: nothing else is meaningful to check.
        return results

    if not found:
        # A product was expected but none was found. This still scores the
        # metrics it missed, rather than skipping them: an early return here
        # would drop this case out of mean_of("product_name_match") etc.
        # entirely, so a prompt that refuses more often would average over a
        # smaller, easier set of cases and look better for refusing more.
        comment = "no product was identified"
        results.append(Evaluation(name="product_name_match", value=0.0, comment=comment))
        if expected_output.get("category_required"):
            results.append(Evaluation(name="category_present", value=0.0, comment=comment))
        if expected_output.get("brand"):
            results.append(Evaluation(name="brand_correct", value=0.0, comment=comment))
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
        Evaluation(
            name="must_include_hit",
            value=1.0 if must <= named else 0.0,
            comment=f"missing {sorted(must - named)}",
        ),
    ]


def mean_of(metric: str):
    """A run-level evaluator: one number for the whole run, per metric."""

    def run_evaluator(*, item_results: list[Any], **kwargs: Any) -> list[Evaluation]:
        values = [
            evaluation.value
            for result in item_results
            for evaluation in getattr(result, "evaluations", [])
            if evaluation.name == metric and isinstance(evaluation.value, int | float)
        ]
        if not values:
            return []
        return [Evaluation(name=f"{metric}_mean", value=sum(values) / len(values))]

    run_evaluator.__name__ = f"{metric}_mean"
    return run_evaluator
