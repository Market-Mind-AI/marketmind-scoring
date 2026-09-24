import uuid
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.catalog_service import CatalogService


def _sem_match() -> AsyncMock:
    """Passo 3 (LLM) desligado: devolve sempre "nenhum candidato serve", que é
    o comportamento a exercitar nos testes de alias/trigram/criação. Injetar
    também garante que a suíte nunca faça uma chamada real de LLM."""
    matcher = AsyncMock()
    matcher.pick.return_value = None
    return matcher


def _match_primeiro(motivo_dado: str = "mesmo produto, nomes diferentes") -> AsyncMock:
    """Passo 3 aceitando o primeiro candidato — simula o LLM reconhecendo o
    produto. O julgamento em si é testado em test_catalog_matcher.py.

    Preenche `motivo` como o matcher real faz: é a evidência da coincidência
    por significado, e é ela que chega ao trace do Langfuse."""

    def _pick(*, text, candidates, motivo=None, category=None):  # noqa: ANN001, ANN202
        if not candidates:
            return None
        if motivo is not None:
            motivo["reasoning"] = motivo_dado
        return candidates[0]

    matcher = AsyncMock()
    matcher.pick.side_effect = _pick
    return matcher


async def test_resolve_creates_new_entity_when_nothing_matches(db_session: AsyncSession) -> None:
    service = CatalogService(db_session, matcher=_sem_match())
    text = f"Brand New Product {uuid.uuid4()}"

    result = await service.resolve(text, source="test")
    assert result.method == "created"
    assert result.canonical_title == text


async def test_resolve_persists_localized_markets_on_new_entity(db_session: AsyncSession) -> None:
    service = CatalogService(db_session, matcher=_sem_match())
    text = f"Jamon Iberico {uuid.uuid4()}"
    localized_markets = {
        "ESP": {
            "primary_name": "jamón ibérico",
            "formal_name": "jamón de bellota",
            "search_terms": ["jamón ibérico", "jamón pata negra"],
            "excluded_terms": ["jamón serrano"],
            "confidence": 0.9,
        },
        "PRT": {
            "primary_name": "presunto pata negra",
            "formal_name": None,
            "search_terms": ["presunto pata negra", "presunto de porco preto"],
            "excluded_terms": [],
            "confidence": 0.8,
        },
    }

    result = await service.resolve(text, source="test", localized_markets=localized_markets)
    assert result.method == "created"

    from app.repositories.product_repository import ProductRepository

    entity = await ProductRepository(db_session).get_by_entity_key(result.entity_key)
    assert entity is not None
    assert entity.localized_markets == localized_markets


async def test_resolve_merges_curated_terms_on_reanalysis(db_session: AsyncSession) -> None:
    # Re-analysis of an existing product (exact alias hit) MERGES its curated
    # terms — never overwrites (2026-07-27): keywords accumulate in the base, and
    # a re-identify only ADDS genuinely new terms (keeping the demand cache
    # stable). A new country is added; an existing country's terms are unioned
    # (existing first + new appended).
    service = CatalogService(db_session, matcher=_sem_match())
    text = f"Presunto {uuid.uuid4()}"
    at_creation = {
        "PRT": {
            "primary_name": "presunto pata negra",
            "formal_name": None,
            "search_terms": ["presunto pata negra"],
            "excluded_terms": [],
            "confidence": 0.7,
        }
    }
    refreshed = {
        "PRT": {  # same country, a NEW term → unioned
            "primary_name": "presunto pata negra",
            "formal_name": None,
            "search_terms": ["presunto ibérico"],
            "excluded_terms": [],
            "confidence": 0.6,
        },
        "ESP": {  # new country → added
            "primary_name": "jamón pata negra",
            "formal_name": None,
            "search_terms": ["jamón pata negra"],
            "excluded_terms": [],
            "confidence": 0.6,
        },
    }
    candidates = [{"country": "ESP", "primary_name": "jamón", "search_terms": ["jamon"], "confidence": 0.9}]

    created = await service.resolve(text, source="test", localized_markets=at_creation)
    again = await service.resolve(text, source="test", localized_markets=refreshed, reference_candidates=candidates)
    assert again.entity_key == created.entity_key  # same entity (exact alias hit)
    assert again.method == "alias"

    from app.repositories.product_repository import ProductRepository

    entity = await ProductRepository(db_session).get_by_entity_key(again.entity_key)
    assert entity is not None
    lm = entity.localized_markets
    assert set(lm) == {"PRT", "ESP"}  # existing country kept, new country added
    # PRT terms unioned (existing first, new appended) — nothing lost
    assert lm["PRT"]["search_terms"] == ["presunto pata negra", "presunto ibérico"]
    assert lm["ESP"]["search_terms"] == ["jamón pata negra"]
    assert entity.reference_candidates == candidates  # newly persisted on re-analysis


async def test_resolve_does_not_wipe_curated_fields_when_none_provided(db_session: AsyncSession) -> None:
    # A text-only resolve (no markets) must NOT wipe existing curated data.
    service = CatalogService(db_session, matcher=_sem_match())
    text = f"Presunto {uuid.uuid4()}"
    at_creation = {
        "PRT": {
            "primary_name": "presunto pata negra",
            "formal_name": None,
            "search_terms": ["presunto pata negra"],
            "excluded_terms": [],
            "confidence": 0.7,
        }
    }
    created = await service.resolve(text, source="test", localized_markets=at_creation)
    await service.resolve(text, source="test")  # no localized_markets

    from app.repositories.product_repository import ProductRepository

    entity = await ProductRepository(db_session).get_by_entity_key(created.entity_key)
    assert entity is not None
    assert entity.localized_markets == at_creation  # preserved


async def test_resolve_second_call_hits_exact_alias(db_session: AsyncSession) -> None:
    service = CatalogService(db_session, matcher=_sem_match())
    text = f"Repeat Product {uuid.uuid4()}"

    first = await service.resolve(text, source="test")
    second = await service.resolve(text, source="test")
    assert second.method == "alias"
    assert second.entity_key == first.entity_key


async def test_resolve_trigram_typo_reuses_entity(db_session: AsyncSession) -> None:
    service = CatalogService(db_session, matcher=_sem_match())
    unique = uuid.uuid4().hex[:8]
    original = f"Portable Blender {unique}"
    typo = f"Portable Blendr {unique}"

    first = await service.resolve(original, source="test")
    second = await service.resolve(typo, source="test")
    assert second.method == "trigram"
    assert second.entity_key == first.entity_key


async def test_resolve_llm_match_reuses_entity(db_session: AsyncSession) -> None:
    """Passo 3: textos sem nenhum parentesco de escrita (alias e trigram
    falham) ainda resolvem para a mesma entidade quando o LLM reconhece o
    produto — é o caso do `quadcopter` para `drone`."""
    first_text = f"Alpha Gadget {uuid.uuid4()}"
    first = await CatalogService(db_session, matcher=_sem_match()).resolve(first_text, source="test")
    assert first.method == "created"

    service = CatalogService(db_session, matcher=_match_primeiro())
    second = await service.resolve(f"Something Totally Different {uuid.uuid4()}", source="test")
    assert second.method == "llm"
    assert second.entity_key == first.entity_key
    # A coincidência por SIGNIFICADO não tem número — `quadcopter`~`drone` não
    # tem semelhança de escrita alguma. O que a explica é a frase.
    assert second.match_score is None
    assert second.match_reasoning == "mesmo produto, nomes diferentes"


async def test_resolve_cria_entidade_quando_llm_nao_reconhece(db_session: AsyncSession) -> None:
    """Sem correspondência no passo 3 — inclusive quando o LLM está fora do ar,
    caso em que CatalogMatcher.pick devolve None — a resolução cria entidade
    nova. É o erro recuperável; uma fusão indevida não seria."""
    service = CatalogService(db_session, matcher=_sem_match())
    result = await service.resolve(f"Produto Inedito {uuid.uuid4()}", source="test")
    assert result.method == "created"


