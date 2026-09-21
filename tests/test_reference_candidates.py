"""The two bounded views of a product's reference-market candidate list.

These exist because the bound used to be three hand-written slices at three call
sites, one of which was missed. The consequence was not a crash: the scoring pass
reached candidates whose demand the coverage pass had never collected, and each
produced a mos_results row marked unavailable for work that should never have
been attempted. The invariant these tests pin down is that the two passes which
must agree do agree, while the demand ceiling keeps its own, deliberately
different, view.
"""

from types import SimpleNamespace

from app.services.analysis_service import (
    _MAX_REFERENCE_CANDIDATES,
    _champion_candidates,
    _top_reference_candidates,
)


def _entity(*countries: str) -> SimpleNamespace:
    return SimpleNamespace(reference_candidates=[{"country": c, "search_terms": [c.lower()]} for c in countries])


def _codes(candidates: list[dict]) -> list[str]:
    return [c["country"] for c in candidates]


# --- the ceiling's view: raw ranked prefix, request-blind -------------------


def test_ceiling_takes_the_raw_prefix_including_requested_countries() -> None:
    # The observed case: an air fryer ranked USA, IND, BRA, and the caller asked
    # about USA and BRA. The ceiling must still measure all three, or the ruler
    # would change with the query and MOS values would stop being comparable
    # between analyses.
    entity = _entity("USA", "IND", "BRA", "IDN", "GBR", "MEX", "ESP", "ITA")
    assert _codes(_top_reference_candidates(entity)) == ["USA", "IND", "BRA"]


def test_ceiling_is_independent_of_what_was_requested() -> None:
    entity = _entity("USA", "IND", "BRA", "IDN")
    # Same answer whatever the request — there is no request parameter at all,
    # which is the structural guarantee, not just the current behaviour.
    assert _codes(_top_reference_candidates(entity)) == ["USA", "IND", "BRA"]


# --- the champion's view: highest-ranked NOT requested ----------------------


def test_champion_skips_requested_and_still_fills_the_bound() -> None:
    # The bug this file exists for: with a shared list, asking about USA and BRA
    # left exactly one usable candidate out of three. Filtering before
    # truncating yields three.
    entity = _entity("USA", "IND", "BRA", "IDN", "GBR", "MEX", "ESP", "ITA")
    assert _codes(_champion_candidates(entity, ["USA", "BRA"])) == ["IND", "IDN", "GBR"]


def test_champion_never_exceeds_the_bound() -> None:
    entity = _entity(*[f"C{i:02d}" for i in range(20)])
    assert len(_champion_candidates(entity, [])) == _MAX_REFERENCE_CANDIDATES


def test_champion_preserves_rank_order() -> None:
    entity = _entity("USA", "IND", "BRA", "IDN")
    assert _codes(_champion_candidates(entity, ["IND"])) == ["USA", "BRA", "IDN"]


def test_champion_matches_country_codes_case_insensitively() -> None:
    # The requested list comes from an API payload; a lowercase code there must
    # not smuggle a requested country back in as a champion.
    entity = _entity("USA", "IND", "BRA")
    assert _codes(_champion_candidates(entity, ["usa"])) == ["IND", "BRA"]


def test_champion_deduplicates_repeated_countries() -> None:
    entity = _entity("IND", "IND", "BRA")
    assert _codes(_champion_candidates(entity, [])) == ["IND", "BRA"]


def test_champion_drops_entries_without_a_country() -> None:
    entity = SimpleNamespace(reference_candidates=[{"search_terms": ["x"]}, {"country": "IND"}])
    assert _codes(_champion_candidates(entity, [])) == ["IND"]


# --- the invariant that was violated ---------------------------------------


def test_coverage_and_scoring_read_the_same_helper() -> None:
    """The regression, caught structurally rather than behaviourally.

    Comparing the two functions' outputs would be tautological — they call the
    same helper, so of course they agree. What actually broke was a call SITE:
    score_reference_candidates iterated the unbounded list while
    ensure_candidates_coverage iterated a bounded one. So this asserts on the
    source: every loop over candidates in those two methods must go through
    _champion_candidates, and the ceiling's must go through the raw helper.
    """
    import inspect
    import re

    from app.services import analysis_service

    src = inspect.getsource(analysis_service)
    expected = {
        "ensure_candidates_coverage": "_champion_candidates",
        "score_reference_candidates": "_champion_candidates",
        "resolve_demand_ceiling": "_top_reference_candidates",
    }
    # Split the module into method bodies so a loop can be attributed to one.
    bodies = re.split(r"\n    (?:async )?def ", src)
    for body in bodies:
        name = body.split("(", 1)[0].strip()
        if name not in expected:
            continue
        loops = re.findall(r"for cand in (\w+)\(", body)
        assert loops, f"{name} no longer loops over candidates — update this test"
        for helper in loops:
            assert helper == expected[name], f"{name} iterates {helper}, expected {expected[name]}"


def test_ceiling_non_requested_members_are_always_collected() -> None:
    """The ceiling measures candidates the champion list may not contain — but
    only requested ones, which are collected under their own localized terms.
    Any NON-requested country the ceiling looks at has to be in the champion
    list, or the ruler would read a series nobody fetched."""
    entity = _entity("USA", "IND", "BRA", "IDN", "GBR", "MEX", "ESP", "ITA")
    for requested in ([], ["USA"], ["USA", "BRA"], ["IND"], ["USA", "IND", "BRA"]):
        requested_set = {c.upper() for c in requested}
        ceiling_unrequested = {c["country"] for c in _top_reference_candidates(entity)} - requested_set
        collected = {c["country"] for c in _champion_candidates(entity, requested)}
        assert ceiling_unrequested <= collected, f"ceiling reads uncollected data for requested={requested}"
