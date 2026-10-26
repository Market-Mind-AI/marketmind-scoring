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


async def test_sinais_de_contexto_saem_em_lista_propria(db_session) -> None:
    """Indicador de contexto nunca tem `normalized_score`, e o laço que monta
    `signals` pula quem não tem score — então `regional_demand_score` e
    `government_expenditure_score` estavam declarados no pacote desde sempre e
    NUNCA saíam nele. O `category_volume_score` herdaria o mesmo destino, que é
    justamente o número que a narrativa precisa para separar "não há mercado
    aqui" de "há mercado e esta marca não está nele".

    Vão numa lista própria, e não misturados: em `signals` todo item tem `score`
    numérico, que é como o frontend os desenha.
    """
    import uuid as _uuid
    from decimal import Decimal

    from app.db.models.indicator_value import IndicatorValue
    from app.db.models.mos_result import MosResult
    from app.repositories.metric_explanation_repository import MetricExplanationRepository
    from app.repositories.product_repository import ProductRepository
    from app.services.explanation_client import ExplanationClient

    repo = ProductRepository(db_session)
    entity = await repo.create_entity(canonical_title=f"whisky {_uuid.uuid4().hex[:8]}")
    await db_session.commit()

    db_session.add(
        IndicatorValue(
            id=_uuid.uuid4(),
            entity_key=entity.entity_key,
            country="GBR",
            indicator_code="category_volume_score",
            period_type="month",
            period_key="2026-08",
            source="pytrends",
            raw_value=Decimal("50000"),
            normalized_score=None,  # contexto puro: não tem régua
            availability_status="available",
            notes="50,000 total monthly searches; brand share 2.0% of the category's generic searches",
        )
    )
    await db_session.commit()

    mos_result = MosResult(
        id=_uuid.uuid4(),
        entity_key=entity.entity_key,
        country="GBR",
        period_type="month",
        period_key="2026-08",
        mos=Decimal("40"),
        mos_interpretation="Low Opportunity",
        availability_status="available",
    )
    client = ExplanationClient(db_session)
    catalog = await MetricExplanationRepository(db_session).catalog()
    pacote = await client._country_evidence(entity.entity_key, mos_result, "2026-08", catalog, {})

    contextos = {c["code"]: c for c in pacote["context_signals"]}
    assert "category_volume_score" in contextos
    assert contextos["category_volume_score"]["value"] == 50000.0
    assert "brand share 2.0%" in contextos["category_volume_score"]["context"]
    # E não entra em `signals`, onde todo item tem score numérico.
    assert all(s["score"] is not None for s in pacote["signals"])


def test_what_changes_per_analysis_stays_out_of_the_cached_part() -> None:
    """The stable part is what the provider caches; a brand or the user's words
    in it would make every analysis a cache write nobody reads."""
    from app.services.explanation_client import _system_parts

    stable, tail = _system_parts(
        "en", single_country=False, user_prompt="Is Spain worth it?", champion=True, brand="Cardhu"
    )
    plain, no_tail = _system_parts("en", single_country=False)

    assert stable == plain
    assert no_tail is None
    assert "Cardhu" in tail and "Is Spain worth it?" in tail
    assert _system_prompt(
        "en", single_country=False, user_prompt="Is Spain worth it?", champion=True, brand="Cardhu"
    ) == f"{stable}\n\n{tail}"
