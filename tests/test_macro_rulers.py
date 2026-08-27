"""As réguas de país, medidas contra a cesta inteira (migration 030).

Os bounds fixos dos indicadores de país foram semeados quando quase nenhum país
tinha dado macro — o próprio `normalization.py` os chama de "plausible starting
points, not perfectly researched limits". Medidos contra os 24 países
suportados, já com os 15 indicadores completos, ocupavam de 32% a 73% da
própria escala.

Espaço morto na régua não é neutro: comprime todos os países numa faixa
estreita do 0-100 e um indicador que SEPARA mercados deixa de separar. O caso
que motivou: Polônia encolhendo 0,82%/ano e Arábia Saudita crescendo 1,74%/ano
saíam como 27 e 59 na mesma escala — e a África do Sul, com investimento de
13,9%, caía ABAIXO do piso de 15 e pontuava 0, lendo como "não investe nada".

Esta suíte prende as duas metades da decisão: as réguas de país recalibradas, e
as de demanda (Google Ads) deliberadamente intocadas — elas dependem do produto,
não do país, e sua régua real é o teto por produto (Cap. 4 §4.2.1).
"""

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.normalization_reference import NormalizationReference
from app.domain.enums import IndicatorCode, IndicatorDirection
from app.services.indicators.normalization import FIXED_BOUNDS_PERIOD_KEY, normalize

# (min, max) esperados por indicador de país — o que a migration 030 semeia.
_REGUAS_DE_PAIS = {
    "population_growth_score": (-1.0, 2.0),
    "gdp_growth_score": (-2.0, 8.0),
    "gdp_per_capita_score": (2000.0, 95000.0),
    "investment_score": (12.0, 35.0),
    "mobile_connectivity_score": (70.0, 190.0),
    "net_exports_score": (-8.0, 12.0),
    "net_migration_rate_score": (-6.0, 9.0),
    "consumer_spending_score": (40.0, 75.0),
    "worldbank_inflation_score": (0.0, 12.0),
}

# Vêm do Google Ads e dependem do PRODUTO, não do país: recalibrá-las contra uma
# população de países seria medir a coisa errada.
_REGUAS_DE_DEMANDA = {
    "trend_score": (0.0, 2_000_000.0),
    "growth_score": (-50.0, 150.0),
    "demand_stability_score": (0.0, 1.0),
}


async def _bounds(session: AsyncSession, code: str) -> tuple[float, float, int]:
    row = (
        await session.execute(
            select(NormalizationReference).where(
                NormalizationReference.indicator_code == code,
                NormalizationReference.period_key == FIXED_BOUNDS_PERIOD_KEY,
            )
        )
    ).scalar_one()
    return float(row.min_x), float(row.max_x), row.reference_population_size


@pytest.mark.parametrize(("code", "esperado"), sorted(_REGUAS_DE_PAIS.items()))
async def test_regua_de_pais_recalibrada(db_session: AsyncSession, code: str, esperado: tuple[float, float]) -> None:
    min_x, max_x, populacao = await _bounds(db_session, code)
    assert (min_x, max_x) == esperado
    # A recalibração só é defensável porque a população de referência existe:
    # 24 países, a cesta suportada inteira, não uma amostra.
    assert populacao == 24


@pytest.mark.parametrize(("code", "esperado"), sorted(_REGUAS_DE_DEMANDA.items()))
async def test_regua_de_demanda_intocada(db_session: AsyncSession, code: str, esperado: tuple[float, float]) -> None:
    min_x, max_x, _ = await _bounds(db_session, code)
    assert (min_x, max_x) == esperado


async def test_africa_do_sul_deixa_de_pontuar_zero_em_investimento(db_session: AsyncSession) -> None:
    """13,9% do PIB caía abaixo do piso antigo (15) e saturava em 0 — o país
    lia como se não investisse nada. Com o piso em 12 ele volta à escala."""
    row = (
        await db_session.execute(
            select(NormalizationReference).where(
                NormalizationReference.indicator_code == IndicatorCode.INVESTMENT_SCORE.value,
                NormalizationReference.period_key == FIXED_BOUNDS_PERIOD_KEY,
            )
        )
    ).scalar_one()
    score, _ = normalize(
        indicator_code=IndicatorCode.INVESTMENT_SCORE,
        raw_value=Decimal("13.88"),
        direction=IndicatorDirection.DIRECT,
        reference=row,
    )
    assert score is not None and score > 0


async def test_inflacao_de_crise_ainda_satura_no_pior_score(db_session: AsyncSession) -> None:
    """O teto de inflação é limiar, não amplitude: 22 dos 24 países vivem entre
    2% e 8%, e as duas economias em crise (Turquia 49%, Argentina 142%) devem
    SATURAR no pior score em vez de esticar a régua para todo mundo — a regra de
    2026-07-24, que a régua mais apertada não pode ter revogado."""
    row = (
        await db_session.execute(
            select(NormalizationReference).where(
                NormalizationReference.indicator_code == IndicatorCode.WORLDBANK_INFLATION_SCORE.value,
                NormalizationReference.period_key == FIXED_BOUNDS_PERIOD_KEY,
            )
        )
    ).scalar_one()
    for inflacao_de_crise in (Decimal("49.1"), Decimal("141.9")):
        score, _ = normalize(
            indicator_code=IndicatorCode.WORLDBANK_INFLATION_SCORE,
            raw_value=inflacao_de_crise,
            direction=IndicatorDirection.INVERSE,
            reference=row,
        )
        assert score == 0.0

    # E a faixa normal ganha escala: 2% e 8% ficam claramente separados.
    baixa, _ = normalize(
        indicator_code=IndicatorCode.WORLDBANK_INFLATION_SCORE,
        raw_value=Decimal("2.03"),
        direction=IndicatorDirection.INVERSE,
        reference=row,
    )
    alta, _ = normalize(
        indicator_code=IndicatorCode.WORLDBANK_INFLATION_SCORE,
        raw_value=Decimal("7.83"),
        direction=IndicatorDirection.INVERSE,
        reference=row,
    )
    assert baixa is not None and alta is not None
    assert baixa - alta > 40
