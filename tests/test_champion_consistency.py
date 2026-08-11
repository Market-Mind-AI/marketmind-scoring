"""The crown must never contradict the numbers next to it.

`is_champion` means "the product's best market, and you did not ask about it".
A report that crowns a market scoring 66 while showing a requested market at 69
is self-contradicting, and that is exactly what shipped: caching the champion
skipped the comparison the uncached path had always done.

The stored champion had beaten the USA by six hundredths of a point (66.37 vs
66.31) in the analysis that computed it. A later analysis scored the USA at 69,
and the cached champion stayed crowned regardless.

These tests pin the comparison down at the level it actually broke — the choice
of champion given a cached value and the current requested scores — without
needing the whole run() pipeline.
"""

from decimal import Decimal
from types import SimpleNamespace

import pytest


def _choose_champion(
    *, cached_country: str | None, cached_mos: Decimal | None, requested: list[tuple[str, Decimal | None]]
) -> str | None:
    """The decision under test, expressed exactly as analysis_service makes it.

    Kept as a local mirror rather than imported because the real code is inline
    in run() and reaching it needs the full pipeline mocked. The structural test
    at the bottom is what keeps the two from drifting apart.
    """
    scored = [(c, m) for c, m in requested if m is not None]
    best_requested = max(scored, key=lambda p: float(p[1]), default=None)

    beats_requested = (
        cached_country is not None
        and cached_mos is not None
        and (best_requested is None or float(cached_mos) > float(best_requested[1]))
    )
    if beats_requested:
        return cached_country
    return best_requested[0] if best_requested is not None else None


def test_cached_champion_loses_to_a_higher_requested_market() -> None:
    """The reported bug, as an assertion.

    BRA cached at 66.37; the USA scores 69 in this analysis. BRA cannot be the
    product's top market here.
    """
    champion = _choose_champion(
        cached_country="BRA",
        cached_mos=Decimal("66.37"),
        requested=[("USA", Decimal("69.0")), ("ESP", Decimal("42.0")), ("PRT", Decimal("39.0"))],
    )
    assert champion == "USA"


def test_cached_champion_survives_when_it_still_beats_everything() -> None:
    """The case the cache exists for: no candidate work, crown preserved."""
    champion = _choose_champion(
        cached_country="BRA",
        cached_mos=Decimal("66.37"),
        requested=[("USA", Decimal("66.31")), ("PRT", Decimal("38.68"))],
    )
    assert champion == "BRA"


def test_a_tie_does_not_crown_the_cached_champion() -> None:
    """Strictly greater, not greater-or-equal. On an exact tie the requested
    market wins, because surfacing an off-request market that merely equals one
    the caller already chose tells them nothing."""
    champion = _choose_champion(
        cached_country="BRA",
        cached_mos=Decimal("70.0"),
        requested=[("USA", Decimal("70.0"))],
    )
    assert champion == "USA"


def test_no_cached_champion_crowns_the_best_requested() -> None:
    champion = _choose_champion(
        cached_country=None,
        cached_mos=None,
        requested=[("USA", Decimal("69.0")), ("ESP", Decimal("42.0"))],
    )
    assert champion == "USA"


def test_nothing_scored_crowns_nothing() -> None:
    """Every requested market unscorable and no cached champion: no crown, rather
    than crowning something arbitrary."""
    assert _choose_champion(cached_country=None, cached_mos=None, requested=[("USA", None)]) is None


def test_cached_champion_wins_when_no_requested_market_scored() -> None:
    """A cached champion is still the answer when nothing requested could be
    scored — it is the only market with a number behind it."""
    champion = _choose_champion(
        cached_country="BRA",
        cached_mos=Decimal("66.37"),
        requested=[("USA", None), ("ESP", None)],
    )
    assert champion == "BRA"


def test_service_compares_the_cached_champion_against_requested_scores() -> None:
    """Structural guard, because the mirror above can drift from the real code.

    What regressed was not a formula but the absence of one: the cached branch
    assigned the stored country and never looked at the current scores. So this
    asserts the comparison is present in that branch.
    """
    import inspect

    from app.services import analysis_service

    src = inspect.getsource(analysis_service.AnalysisService.run)
    assert "best_requested" in src, "the cached champion branch no longer computes the best requested market"
    assert "beats_requested" in src, "the cached champion is no longer compared against requested scores"
    # And the comparison must be strict, so a tie favours the requested market.
    assert "float(cached_mos) > float(best_requested.mos)" in src, (
        "the cached-vs-requested comparison changed shape; confirm a tie still favours the requested market"
    )


@pytest.mark.parametrize(
    ("cached", "requested_top", "expected"),
    [
        (Decimal("66.37"), Decimal("69.00"), "USA"),  # reported bug
        (Decimal("69.01"), Decimal("69.00"), "BRA"),  # margin as small as the original 0.06
        (Decimal("0.00"), Decimal("1.00"), "USA"),
        (Decimal("100.00"), Decimal("99.99"), "BRA"),
    ],
)
def test_boundaries(cached: Decimal, requested_top: Decimal, expected: str) -> None:
    assert _choose_champion(cached_country="BRA", cached_mos=cached, requested=[("USA", requested_top)]) == expected
