import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import EntityNotFoundError, MarketMindCoreError
from app.domain.enums import AvailabilityStatus, IndicatorDirection
from app.repositories.product_repository import ProductRepository
from app.services.indicator_engine import IndicatorEngine
from app.services.indicators.results import RawIndicatorResult

_ENGINE = "app.services.indicator_engine"
_AVAILABLE_WB = RawIndicatorResult(50.0, IndicatorDirection.DIRECT, "y_latest", AvailabilityStatus.AVAILABLE, None)
_AVAILABLE_INVERSE = RawIndicatorResult(2.0, IndicatorDirection.INVERSE, "last_3y", AvailabilityStatus.AVAILABLE, None)
_UNAVAILABLE = RawIndicatorResult(
    None, IndicatorDirection.DIRECT, "y_latest", AvailabilityStatus.UNAVAILABLE, "no data"
)
_CONDITIONAL_UNAVAILABLE = RawIndicatorResult(
    None, IndicatorDirection.DIRECT, "last_12q", AvailabilityStatus.CONDITIONAL, "insufficient RHP quarters"
)

_COUNTRY_LEVEL_TARGETS = [
    f"{_ENGINE}.worldbank_indicators.compute_gdp_per_capita",
    f"{_ENGINE}.worldbank_indicators.compute_internet_penetration",
    f"{_ENGINE}.worldbank_indicators.compute_mobile_connectivity",
    # Estreou com a migration 044, ao lado do chip. Sem o dublê o motor sai
    # para a rede de verdade e sete testes falham com erro de DNS, o que não
    # se lê como "faltou um mock".
    f"{_ENGINE}.worldbank_indicators.compute_broadband_adoption",
    f"{_ENGINE}.worldbank_indicators.compute_gdp_growth",
    f"{_ENGINE}.worldbank_indicators.compute_population_scale",
    f"{_ENGINE}.worldbank_indicators.compute_urbanization",
    f"{_ENGINE}.undata_indicators.compute_population_growth",
    f"{_ENGINE}.undata_indicators.compute_net_migration_rate",
    # Macro Demand Structure + Real Estate Growth (added 2026-07-08)
    f"{_ENGINE}.worldbank_indicators.compute_consumer_spending",
    f"{_ENGINE}.worldbank_indicators.compute_government_expenditure",
    f"{_ENGINE}.worldbank_indicators.compute_investment",
    f"{_ENGINE}.worldbank_indicators.compute_net_exports",
    f"{_ENGINE}.worldbank_indicators.compute_trade_openness",
]

# Demand from Google Ads absolute search volume (2026-07-15): 4 indicators,
# no regional_demand and no granularity fallback.
_PYTRENDS_TARGETS = {
    "trend": f"{_ENGINE}.pytrends_indicators.compute_trend_score",
    "growth": f"{_ENGINE}.pytrends_indicators.compute_growth_score",
    "stability": f"{_ENGINE}.pytrends_indicators.compute_demand_stability_score",
    "intent": f"{_ENGINE}.pytrends_indicators.compute_search_intent_score",
}


def _unique_period_key() -> str:
    """Not a real 'YYYY-MM' period — fine here since every pytrends compute_*
    call is mocked in this file, so nothing ever parses it as a date. Unique
    per test so reruns against a persistent (not recreated) dev DB never
    collide with indicator_values rows a previous run already committed for a
    fixed literal like "2026-07". (Fixed normalization bounds are keyed on
    period_key="FIXED", independent of this.)"""
    return f"2026-07-test-{uuid.uuid4().hex[:8]}"


@contextmanager
def _patched_country_level(*, inflation: RawIndicatorResult = _AVAILABLE_INVERSE) -> Iterator[None]:
    with ExitStack() as stack:
        for target in _COUNTRY_LEVEL_TARGETS:
            stack.enter_context(patch(target, return_value=_AVAILABLE_WB))
        stack.enter_context(patch(f"{_ENGINE}.worldbank_indicators.compute_inflation", return_value=inflation))
        stack.enter_context(
            patch(f"{_ENGINE}.oecd_indicators.compute_real_estate_growth", return_value=_CONDITIONAL_UNAVAILABLE)
        )
        yield


@contextmanager
def _patched_pytrends(**overrides: RawIndicatorResult | list[RawIndicatorResult]) -> Iterator[None]:
    with ExitStack() as stack:
        for key, target in _PYTRENDS_TARGETS.items():
            value = overrides.get(key, _UNAVAILABLE)
            if isinstance(value, list):
                stack.enter_context(patch(target, side_effect=value))
            else:
                stack.enter_context(patch(target, return_value=value))
        stack.enter_context(patch(f"{_ENGINE}.undata_indicators.compute_median_age_fit", return_value=_UNAVAILABLE))
        # trend_score enrichment fetches population to compute penetration
        # context (searches per 100k inhabitants) — patch it so it never hits
        # the external World Bank DB.
        stack.enter_context(patch(f"{_ENGINE}.worldbank_indicators.population", return_value=1_000_000.0))
        yield


async def test_unknown_country_raises(db_session: AsyncSession) -> None:
    engine = IndicatorEngine(db_session)
    with pytest.raises(MarketMindCoreError):
        await engine.compute_all(country_iso3="ZZZ")


async def test_unknown_entity_raises(db_session: AsyncSession) -> None:
    engine = IndicatorEngine(db_session)
    with _patched_country_level(), pytest.raises(EntityNotFoundError):
        await engine.compute_all(country_iso3="USA", entity_key=uuid.uuid4(), period_key=_unique_period_key())


async def test_country_level_inflation_from_worldbank_only(db_session: AsyncSession) -> None:
    # World Bank is the sole inflation source now (OECD path removed
    # 2026-07-08); it's AVAILABLE like any other WB indicator, no OECD
    # inflation codes ever appear in the output.
    period_key = _unique_period_key()
    # Este teste NÃO semeia o bound FIXED de gdp_per_capita_score, de propósito.
    # `normalization_reference` está em _PRESERVE_TABLES (conftest), ou seja,
    # nunca é truncada entre testes: um upsert em period_key="FIXED" aqui
    # sobrevive ao teste e passa a valer para todos os seguintes. Foi o que
    # acontecia — a régua semeada pela migration virava (500, 100000) para o
    # resto da suíte, e só aparecia como falha quando algum teste posterior
    # afirmava o valor real. O bound vem das migrations, que a conftest já
    # exige aplicadas; o que se mede aqui é disponibilidade, não score.
    engine = IndicatorEngine(db_session)
    with _patched_country_level():
        results = await engine.compute_all(country_iso3="USA", period_key=period_key)

    by_code = {r.indicator_code: r for r in results}
    assert by_code["worldbank_inflation_score"].availability_status == "available"
    assert "oecd_inflation_level_score" not in by_code
    assert "oecd_inflation_volatility_score" not in by_code
    # gdp_per_capita_score has a FIXED reference row -> normalized.
    assert by_code["gdp_per_capita_score"].normalized_score is not None
    assert all(r.entity_key is None for r in results)


async def test_product_specific_indicators_computed_with_entity_key(db_session: AsyncSession) -> None:
    # Demand is now read per country from the product's approved localized terms
    # (product_catalog.localized_markets[ISO3]); the engine saves 4 pytrends
    # indicators (trend/growth/stability/intent — no regional_demand) plus the
    # conditional median-age fit.
    entity = await ProductRepository(db_session).create_entity(
        canonical_title=f"Air Fryer {uuid.uuid4()}",
        localized_markets={"USA": {"primary_name": "air fryer", "search_terms": ["air fryer", "airfryer"]}},
    )
    await db_session.commit()

    trend = RawIndicatorResult(60.0, IndicatorDirection.DIRECT, "2026-07", AvailabilityStatus.AVAILABLE, None)

    engine = IndicatorEngine(db_session)
    with _patched_country_level(), _patched_pytrends(trend=trend):
        results = await engine.compute_all(
            country_iso3="USA", entity_key=entity.entity_key, period_key=_unique_period_key()
        )

    by_code = {r.indicator_code: r for r in results if r.entity_key is not None}
    assert by_code["trend_score"].raw_value == 60
    assert by_code["trend_score"].entity_key == entity.entity_key
    # 4 pytrends Demand indicators + median-age fit, all keyed to the entity.
    assert {"trend_score", "growth_score", "demand_stability_score", "search_intent_score"} <= set(by_code)
    assert "regional_demand_score" not in by_code
    assert by_code["median_age_fit_score"].entity_key == entity.entity_key
    country_level = [r for r in results if r.entity_key is None]
    assert len(country_level) >= 8


async def test_categoria_e_medida_como_contexto_ao_lado_da_marca(db_session: AsyncSession) -> None:
    """Decisão D4: a análise de um produto COM marca mede também a categoria, e o
    número da categoria é contexto puro — nunca pontuado.

    É o que separa "não existe mercado aqui" de "existe mercado e esta marca não
    está nele", que era a leitura impossível no relatório da Cardhu: 24 países
    com demanda baixa e nada dizendo se whisky vendia ou não naqueles mercados.
    """
    from app.domain.enums import IndicatorCode
    from app.repositories.product_repository import ProductRepository
    from app.services.mos.dimension_mapping import DIMENSION_BY_INDICATOR

    sufixo = uuid.uuid4().hex[:8]
    repo = ProductRepository(db_session)
    familia = await repo.create_entity(
        canonical_title=f"single malt whisky {sufixo}",
        localized_markets={"USA": {"primary_name": "single malt whisky", "search_terms": ["single malt whisky"]}},
    )
    marca = await repo.get_or_create_brand(name=f"Cardhu{sufixo}", source="test")
    entidade = await repo.create_entity(
        canonical_title=f"Cardhu single malt whisky {sufixo}",
        localized_markets={"USA": {"primary_name": "cardhu 12", "search_terms": ["cardhu 12"]}},
        brand_key=marca.brand_key,
        generic_entity_key=familia.entity_key,
    )
    await db_session.commit()

    volume_marca = RawIndicatorResult(
        1_000.0, IndicatorDirection.DIRECT, "2026-08", AvailabilityStatus.AVAILABLE, "1,000 total monthly searches"
    )
    volume_categoria = RawIndicatorResult(
        50_000.0, IndicatorDirection.DIRECT, "2026-08", AvailabilityStatus.AVAILABLE, "50,000 total monthly searches"
    )

    engine = IndicatorEngine(db_session)
    # A primeira chamada é a da marca, a segunda a da categoria.
    # marca (com expansão) -> categoria -> marca CURADA, que é a que entra na razão
    with _patched_country_level(), _patched_pytrends(trend=[volume_marca, volume_categoria, volume_marca]):
        results = await engine.compute_all(
            country_iso3="USA",
            entity_key=entidade.entity_key,
            period_key=_unique_period_key(),
            demand_ceiling=1_000.0,
            brand_key=marca.brand_key,
        )

    by_code = {r.indicator_code: r for r in results if r.entity_key is not None}
    categoria = by_code[IndicatorCode.CATEGORY_VOLUME_SCORE.value]

    assert float(categoria.raw_value) == 50_000.0
    assert categoria.normalized_score is None, "contexto puro: não tem régua, não tem score"
    assert IndicatorCode.CATEGORY_VOLUME_SCORE not in DIMENSION_BY_INDICATOR, "não entra em dimensão nenhuma"
    # O share vem calculado aqui, determinístico, com os dois números à vista.
    assert "the brand is 2% of that (1,000 vs 50,000)" in categoria.notes
    # A marca segue pontuada normalmente, sem contaminação.
    assert float(by_code[IndicatorCode.TREND_SCORE.value].raw_value) == 1_000.0
    assert by_code[IndicatorCode.TREND_SCORE.value].normalized_score is not None


async def test_produto_sem_marca_nao_mede_categoria(db_session: AsyncSession) -> None:
    """Numa entidade genérica não há categoria acima dela: o indicador de
    contexto simplesmente não existe, em vez de duplicar o próprio número."""
    from app.domain.enums import IndicatorCode
    from app.repositories.product_repository import ProductRepository

    entidade = await ProductRepository(db_session).create_entity(
        canonical_title=f"air fryer {uuid.uuid4().hex[:8]}",
        localized_markets={"USA": {"primary_name": "air fryer", "search_terms": ["air fryer"]}},
    )
    await db_session.commit()

    trend = RawIndicatorResult(
        900.0, IndicatorDirection.DIRECT, "2026-08", AvailabilityStatus.AVAILABLE, "900 total monthly searches"
    )
    engine = IndicatorEngine(db_session)
    with _patched_country_level(), _patched_pytrends(trend=trend):
        results = await engine.compute_all(
            country_iso3="USA", entity_key=entidade.entity_key, period_key=_unique_period_key()
        )

    assert IndicatorCode.CATEGORY_VOLUME_SCORE.value not in {r.indicator_code for r in results}


async def test_marca_maior_que_a_categoria_e_dita_como_razao(db_session: AsyncSession) -> None:
    """Medido ao vivo em 27/08: a Cardhu na Espanha tem 9.630 buscas contra 3.140
    dos termos genéricos da categoria. O denominador são os termos GENÉRICOS, não
    a demanda total do mercado — que incluiria as outras marcas e ninguém mede —
    então "share de 306%" seria uma frase falsa saindo de um número correto."""
    from app.domain.enums import IndicatorCode
    from app.repositories.product_repository import ProductRepository

    sufixo = uuid.uuid4().hex[:8]
    repo = ProductRepository(db_session)
    # UMA entidade: o produto. O vocabulário da CATEGORIA fica nela; o da
    # MARCA, em brand_markets.
    familia = await repo.create_entity(
        canonical_title=f"single malt whisky {sufixo}",
        localized_markets={"USA": {"primary_name": "single malt whisky", "search_terms": ["single malt whisky"]}},
    )
    marca = await repo.get_or_create_brand(name=f"Cardhu{sufixo}", source="test")
    entidade = familia
    await repo.save_brand_markets(
        entidade.entity_key,
        marca.brand_key,
        {"USA": {"primary_name": "cardhu", "search_terms": ["cardhu"]}},
    )
    await db_session.commit()

    volume_marca = RawIndicatorResult(
        9_630.0, IndicatorDirection.DIRECT, "2026-08", AvailabilityStatus.AVAILABLE, "9,630 total monthly searches"
    )
    volume_categoria = RawIndicatorResult(
        3_140.0, IndicatorDirection.DIRECT, "2026-08", AvailabilityStatus.AVAILABLE, "3,140 total monthly searches"
    )
    engine = IndicatorEngine(db_session)
    with _patched_country_level(), _patched_pytrends(trend=[volume_marca, volume_categoria, volume_marca]):
        results = await engine.compute_all(
            country_iso3="USA",
            entity_key=entidade.entity_key,
            period_key=_unique_period_key(),
            demand_ceiling=9_630.0,
            brand_key=marca.brand_key,
        )

    notas = {r.indicator_code: r.notes for r in results if r.entity_key is not None}
    categoria = notas[IndicatorCode.CATEGORY_VOLUME_SCORE.value]
    assert "3.1x MORE than the generic category terms" in categoria
    assert "9,630 vs 3,140" in categoria
    assert "share" not in categoria.lower(), "share implica parte-de-um-todo que o denominador não é"


async def test_razao_compara_termos_curados_dos_dois_lados(db_session: AsyncSession) -> None:
    """O Demand Volume pontuado da marca soma a expansão (é a régua dela) e a
    categoria não soma. Usar aquele número na razão compararia "curados + 15
    relacionadas" contra "curados" — medido ao vivo no Reino Unido, isso dizia
    "a marca é 1,1x MAIOR que a categoria" quando os curados dos dois lados
    dizem o contrário."""
    from app.domain.enums import IndicatorCode
    from app.repositories.product_repository import ProductRepository

    sufixo = uuid.uuid4().hex[:8]
    repo = ProductRepository(db_session)
    # UMA entidade: o produto. O vocabulário da CATEGORIA fica nela; o da
    # MARCA, em brand_markets.
    familia = await repo.create_entity(
        canonical_title=f"single malt whisky {sufixo}",
        localized_markets={"USA": {"primary_name": "single malt whisky", "search_terms": ["single malt whisky"]}},
    )
    marca = await repo.get_or_create_brand(name=f"Cardhu{sufixo}", source="test")
    entidade = familia
    await repo.save_brand_markets(
        entidade.entity_key,
        marca.brand_key,
        {"USA": {"primary_name": "cardhu", "search_terms": ["cardhu"]}},
    )
    await db_session.commit()

    def _raw(v: float) -> RawIndicatorResult:
        return RawIndicatorResult(
            v, IndicatorDirection.DIRECT, "2026-08", AvailabilityStatus.AVAILABLE, f"{v:,.0f} total monthly searches"
        )

    # Marca COM expansão 20.980; categoria 18.460; marca só com os curados 14.000.
    engine = IndicatorEngine(db_session)
    with _patched_country_level(), _patched_pytrends(trend=[_raw(20_980), _raw(18_460), _raw(14_000)]):
        results = await engine.compute_all(
            country_iso3="USA",
            entity_key=entidade.entity_key,
            period_key=_unique_period_key(),
            demand_ceiling=20_980.0,
            brand_key=marca.brand_key,
        )

    por_codigo = {r.indicator_code: r for r in results if r.entity_key is not None}
    # O score da marca segue usando o número COM expansão — a régua dela não muda.
    assert float(por_codigo[IndicatorCode.TREND_SCORE.value].raw_value) == 20_980.0
    # A razão usa os curados dos dois lados: 14.000 / 18.460 = 76%.
    categoria = por_codigo[IndicatorCode.CATEGORY_VOLUME_SCORE.value].notes
    assert "the brand is 76% of that (14,000 vs 18,460)" in categoria
    assert "CURATED terms" in categoria
