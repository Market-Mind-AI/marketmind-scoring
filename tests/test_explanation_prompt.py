"""The final-narrative system prompt must adapt to how many countries were
analyzed: a SINGLE-country analysis is a standalone assessment, NOT a ranking,
so the prompt must carry no ranking/ordering/comparison framing (only the
negative instruction telling the model to avoid it)."""

from app.services.explanation_client import _system_prompt

# Ranking/comparison framing phrases that must NOT appear as instructions in a
# single-country prompt (they are fine — required, even — in the multi prompt).
# NB: "priority order" / "ordem de prioridade" alone would match the *negative*
# instruction ("do not use priority-order language"), so we assert on the fuller
# framing phrases that only appear when the prompt is actually asking to rank.
_PT_FRAMING = ("em quais países", "na ordem do mos", "ordem de prioridade dos países pelo mos")
_EN_FRAMING = ("in which countries", "ordered by mos", "the countries' priority order")


def test_single_country_prompt_has_no_ranking_framing_pt() -> None:
    s = _system_prompt("pt", single_country=True).lower()
    assert "{" not in s  # every placeholder filled
    for phrase in _PT_FRAMING:
        assert phrase not in s, f"single-country PT prompt leaks ranking framing: {phrase!r}"


def test_single_country_prompt_has_no_ranking_framing_en() -> None:
    s = _system_prompt("en", single_country=True).lower()
    assert "{" not in s
    for phrase in _EN_FRAMING:
        assert phrase not in s, f"single-country EN prompt leaks ranking framing: {phrase!r}"


def test_multi_country_prompt_keeps_ranking_framing() -> None:
    # The multi-country prompt SHOULD still instruct ordering by MOS.
    assert "na ordem do mos" in _system_prompt("pt", single_country=False).lower()
    assert "ordered by mos" in _system_prompt("en", single_country=False).lower()


def test_single_country_prompt_instructs_against_comparison() -> None:
    # The negative instruction is expected (it tells the model NOT to compare).
    assert "apenas um país" in _system_prompt("pt", single_country=True).lower()
    assert "only one country" in _system_prompt("en", single_country=True).lower()
