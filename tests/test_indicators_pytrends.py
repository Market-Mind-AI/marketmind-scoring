"""Demand raw-value functions, now sourced from Google Ads ABSOLUTE search
volume (DataForSEO) instead of Google Trends (2026-07-15). The four signals
read the collector's own `keyword_search_volume` / `keyword_related` tables:
LEVEL = ln(SUM of the country's terms' volume); GROWTH/STABILITY from the summed
monthly series; SEARCH INTENT from volume-weighted related-keyword classification.
"""

import json
from typing import Any
from unittest.mock import patch

import pytest

from app.domain.enums import AvailabilityStatus, IndicatorDirection
from app.services.indicators import pytrends as pytrends_indicators
from tests._external_db_mock import make_mock_engine, result_with_all


def _months(volumes: list[float], *, year: int = 2025, start_month: int = 1) -> list[dict[str, Any]]:
    """Build a keyword_search_volume.monthly_searches JSONB value (a list of
    {year, month, search_volume} dicts) from a bare list of monthly volumes,
    advancing month-by-month (wrapping the year after December)."""
    out: list[dict[str, Any]] = []
    y, m = year, start_month
    for vol in volumes:
        out.append({"year": y, "month": m, "search_volume": vol})
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


# --- LEVEL (compute_trend_score) -------------------------------------------


async def test_trend_score_sums_volume_linear() -> None:
    # DEMAND VOLUME = SUM of the country's distinct terms' search_volume (raw is
    # the absolute count, normalized linearly downstream). Two genuinely distinct
    # terms are both kept and summed: 1000 + 500 = 1500.
    rows = [
        ("air fryer", 1000.0, _months([100.0] * 12)),
        ("freidora de aire", 500.0, _months([50.0] * 12)),
    ]
    engine = make_mock_engine([result_with_all(rows), result_with_all([])])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        result = await pytrends_indicators.compute_trend_score(["air fryer", "freidora de aire"], "ES", "2026-07")

    assert result.availability_status == AvailabilityStatus.AVAILABLE
    assert result.direction == IndicatorDirection.DIRECT
    assert result.raw_value == pytest.approx(1500.0)


async def test_trend_score_dedups_identical_rows() -> None:
    # Google Ads canonicalizes near-variants ('air fryer'/'airfryer') and reports
    # the SAME (search_volume, monthly_searches) — summing raw would double-count,
    # so identical rows are deduped to a single contribution.
    monthly = _months([100.0] * 12)
    rows = [
        ("air fryer", 1000.0, monthly),
        ("airfryer", 1000.0, monthly),  # identical numbers -> deduped away
    ]
    engine = make_mock_engine([result_with_all(rows), result_with_all([])])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        result = await pytrends_indicators.compute_trend_score(["air fryer", "airfryer"], "ES", "2026-07")

    assert result.availability_status == AvailabilityStatus.AVAILABLE
    # Only 1000 counted once: 1000, not 2000.
    assert result.raw_value == pytest.approx(1000.0)


async def test_trend_score_expands_with_product_related_drops_informational() -> None:
    # Expansion folds high-volume RELATED that are the product (contain a product
    # term) AND purchase-oriented; it drops usage/curiosity (recipe/what-is =
    # ownership/browsing, not new demand) and unrelated products. No excluded list.
    approved = [("air fryer", 1000.0, _months([100.0] * 12))]
    related = [  # (keyword, volume, monthly, cpc, competition_index)
        ("air fryer ninja", 3000.0, _months([300.0] * 12), 1.2, 100),  # product -> folded
        ("air fryer recipe", 9000.0, _months([900.0] * 12), 0.1, 20),  # usage -> dropped
        ("what is an air fryer", 5000.0, _months([500.0] * 12), 0.1, 5),  # curiosity -> dropped
        ("blender", 8000.0, _months([800.0] * 12), 1.0, 100),  # not the product -> dropped
    ]
    engine = make_mock_engine([result_with_all(approved), result_with_all(related)])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        result = await pytrends_indicators.compute_trend_score(["air fryer"], "US", "2026-07", language="en")

    assert result.availability_status == AvailabilityStatus.AVAILABLE
    # 1000 (approved) + 3000 (ninja); recipe / what-is / blender all excluded.
    assert result.raw_value == pytest.approx(4000.0)
    assert result.notes is not None and "+1 folded" in result.notes


async def test_trend_score_collapses_google_variant_duplicates() -> None:
    # Google reports the SAME 301k under 'Airfryer' (approved), 'airfryer' (related,
    # from a different table with a slightly different series) and the typo
    # 'airfryert' — it's ONE query, counted once. A genuinely distinct product
    # ('cosori airfryer', different volume) is summed.
    approved = [("Airfryer", 301000.0, _months([25000.0] * 12))]
    related = [  # (keyword, volume, monthly, cpc, competition_index)
        ("airfryer", 301000.0, _months([25010.0] * 12), 0.8, 100),  # same term, other table/series
        ("airfryert", 301000.0, _months([25000.0] * 12), 0.8, 100),  # typo close-variant
        ("cosori airfryer", 50000.0, _months([4000.0] * 12), 0.9, 100),  # distinct product -> summed
    ]
    engine = make_mock_engine([result_with_all(approved), result_with_all(related)])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        result = await pytrends_indicators.compute_trend_score(["Airfryer"], "DE", "2026-07", language="de")

    assert result.availability_status == AvailabilityStatus.AVAILABLE
    # 301000 counted ONCE + 50000, not 301000 x3 + 50000.
    assert result.raw_value == pytest.approx(351000.0)


async def test_trend_score_accepts_json_string_monthly_searches() -> None:
    # monthly_searches may arrive as a JSON string (raw JSONB) — parsed the same.
    rows = [("air fryer", 200.0, json.dumps(_months([20.0] * 12)))]
    engine = make_mock_engine([result_with_all(rows), result_with_all([])])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        result = await pytrends_indicators.compute_trend_score(["air fryer"], "ES", "2026-07")

    assert result.availability_status == AvailabilityStatus.AVAILABLE
    assert result.raw_value == pytest.approx(200.0)


async def test_trend_score_unavailable_when_no_rows() -> None:
    engine = make_mock_engine([result_with_all([]), result_with_all([])])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        result = await pytrends_indicators.compute_trend_score(["air fryer"], "ES", "2026-07")

    assert result.availability_status == AvailabilityStatus.UNAVAILABLE
    assert result.raw_value is None


# --- GROWTH (compute_growth_score) -----------------------------------------


async def test_growth_score_computes_expected_raw() -> None:
    # One term's monthly series [10,10,10,20,20,20,40,40,40]: n=9, k=3,
    # initial_avg=10, final_avg=40 -> ((40-10)/10)*100 = 300.
    rows = [("air fryer", 200.0, _months([10, 10, 10, 20, 20, 20, 40, 40, 40]))]
    engine = make_mock_engine([result_with_all(rows), result_with_all([])])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        result = await pytrends_indicators.compute_growth_score(["air fryer"], "ES", "2026-07")

    assert result.availability_status == AvailabilityStatus.AVAILABLE
    assert result.raw_value == pytest.approx(300.0)


async def test_growth_score_sums_series_across_distinct_terms() -> None:
    # Growth runs on the ELEMENT-WISE sum of the distinct terms' monthly series,
    # aligned by (year, month). Two distinct rows (different numbers -> both kept):
    # A=[20]*6 and B=[0,0,0,30,30,30] over the same months -> summed
    # [20,20,20,50,50,50]. n=6, k=2, initial_avg=20, final_avg=50 -> 150%.
    rows = [
        ("air fryer", 120.0, _months([20, 20, 20, 20, 20, 20])),
        ("freidora de aire", 90.0, _months([0, 0, 0, 30, 30, 30])),
    ]
    engine = make_mock_engine([result_with_all(rows), result_with_all([])])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        result = await pytrends_indicators.compute_growth_score(["air fryer", "freidora de aire"], "ES", "2026-07")

    assert result.availability_status == AvailabilityStatus.AVAILABLE
    assert result.raw_value == pytest.approx(150.0)


async def test_growth_score_unavailable_with_fewer_than_six_months() -> None:
    rows = [("air fryer", 60.0, _months([10, 20, 30]))]
    engine = make_mock_engine([result_with_all(rows), result_with_all([])])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        result = await pytrends_indicators.compute_growth_score(["air fryer"], "ES", "2026-07")

    assert result.availability_status == AvailabilityStatus.UNAVAILABLE
    assert result.raw_value is None


async def test_growth_score_flat_zero_on_near_zero_baseline() -> None:
    # Low-base noise guard: a near-zero initial window makes the ratio explode.
    # Must return a flat 0% (AVAILABLE), not a spurious extreme nor UNAVAILABLE.
    rows = [("air fryer", 153.0, _months([0, 0, 1, 0, 0, 2, 40, 50, 60]))]
    engine = make_mock_engine([result_with_all(rows), result_with_all([])])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        result = await pytrends_indicators.compute_growth_score(["air fryer"], "ES", "2026-07")

    assert result.availability_status == AvailabilityStatus.AVAILABLE
    assert result.raw_value == 0.0


# --- DEMAND STABILITY (compute_demand_stability_score) ----------------------


async def test_demand_stability_score_computes_cv() -> None:
    rows = [("air fryer", 60.0, _months([10, 20, 30]))]
    engine = make_mock_engine([result_with_all(rows), result_with_all([])])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        result = await pytrends_indicators.compute_demand_stability_score(["air fryer"], "ES", "2026-07")

    assert result.availability_status == AvailabilityStatus.AVAILABLE
    assert result.direction == IndicatorDirection.INVERSE
    # mean=20, population stddev=sqrt(200/3)=8.1650, CV=8.1650/20
    assert result.raw_value == pytest.approx(0.408248, rel=1e-4)


async def test_demand_stability_neutral_on_low_volume_series() -> None:
    # Low-volume noise guard: a near-zero, spiky series (mean < 10) can't yield a
    # reliable CV, so return a NEUTRAL score instead of a spurious extreme.
    # (volume 15 clears the term-drop floor; the SERIES mean is what's near-zero.)
    rows = [("presunto pata negra", 15.0, _months([0, 0, 2, 0, 0, 1]))]
    engine = make_mock_engine([result_with_all(rows), result_with_all([])])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        result = await pytrends_indicators.compute_demand_stability_score(["presunto pata negra"], "PT", "2026-07")

    assert result.availability_status == AvailabilityStatus.AVAILABLE
    assert result.raw_value == pytest.approx(0.5)  # neutral CV
    assert result.notes is not None and "low-volume" in result.notes


async def test_demand_stability_unavailable_with_fewer_than_two_months() -> None:
    rows = [("air fryer", 10.0, _months([10]))]
    engine = make_mock_engine([result_with_all(rows), result_with_all([])])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        result = await pytrends_indicators.compute_demand_stability_score(["air fryer"], "ES", "2026-07")

    assert result.availability_status == AvailabilityStatus.UNAVAILABLE
    assert result.raw_value is None


# --- SEARCH INTENT (compute_search_intent_score + _classify_query_intent) ---


def test_classify_query_intent_tiers() -> None:
    # Deterministic unit test with a controlled lexicon (no dependency on the
    # generated one): each tier returns its weight; unmatched -> default.
    tiers = {
        "transactional": ["buy", "amazon"],
        "curiosity": ["what is", "vs"],
        "evaluation": ["best", "review"],
        "usage": ["how to", "recipe"],
    }
    classify = pytrends_indicators._classify_query_intent
    assert classify("buy iberico ham", tiers) == 1.0
    assert classify("iberico ham vs prosciutto", tiers) == 0.2  # curiosity
    assert classify("what is iberico ham", tiers) == 0.2
    assert classify("best iberico ham", tiers) == 0.8
    assert classify("how to cook iberico ham", tiers) == 0.4
    assert classify("jamon cebo iberico", tiers) == 0.6  # unmatched variant -> default


async def test_search_intent_uses_language_lexicon() -> None:
    # Integration: classified with the EN lexicon (buy -> transactional 1.0,
    # what is -> curiosity 0.2), weighted by real volume. cpc/competition NULL,
    # so no commercial signal -> pure lexical (backward-compatible path).
    rows = [("buy iberico ham", 80.0, None, None, None), ("what is iberico ham", 20.0, None, None, None)]
    engine = make_mock_engine([result_with_all(rows), result_with_all([])])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        result = await pytrends_indicators.compute_search_intent_score(["iberico ham"], "ES", "2026-07", language="en")

    assert result.availability_status == AvailabilityStatus.AVAILABLE
    # buy 1.0*80 + what-is 0.2*20 = 84; /100 *100
    assert result.raw_value == pytest.approx(84.0)


async def test_search_intent_blends_commercial_signal() -> None:
    # With cpc/competition present, the commercial signal blends with the lexical
    # tier (0.6 lexical + 0.4 commercial). Single query "air fryer" -> lexical
    # DEFAULT 0.6; competition_index 100 (=1.0), no cpc -> commercial 1.0.
    # intent = 0.6*0.6 + 0.4*1.0 = 0.76 -> 76.0.
    rows = [("air fryer", 500.0, None, None, 100)]
    engine = make_mock_engine([result_with_all(rows), result_with_all([])])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        result = await pytrends_indicators.compute_search_intent_score(["air fryer"], "US", "2026-07", language="en")

    assert result.availability_status == AvailabilityStatus.AVAILABLE
    assert result.raw_value == pytest.approx(76.0)


async def test_search_intent_curiosity_scores_low() -> None:
    # A market only browsing ("what is / vs") scores low.
    rows = [("what is iberico ham", 100.0, None, None, None), ("iberico ham vs prosciutto", 100.0, None, None, None)]
    engine = make_mock_engine([result_with_all(rows), result_with_all([])])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        result = await pytrends_indicators.compute_search_intent_score(["iberico ham"], "ES", "2026-07", language="en")

    assert result.raw_value == pytest.approx(20.0)


async def test_search_intent_unknown_language_falls_back_to_english() -> None:
    rows = [("buy iberico ham", 100.0, None, None, None)]
    engine = make_mock_engine([result_with_all(rows), result_with_all([])])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        result = await pytrends_indicators.compute_search_intent_score(["iberico ham"], "ES", "2026-07", language="xx")

    assert result.raw_value == pytest.approx(100.0)  # "buy" still transactional via EN fallback


async def test_search_intent_unavailable_when_no_seed_terms() -> None:
    # Empty seeds -> UNAVAILABLE without ever hitting the DB.
    engine = make_mock_engine([])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        result = await pytrends_indicators.compute_search_intent_score([], "ES", "2026-07", language="en")

    assert result.availability_status == AvailabilityStatus.UNAVAILABLE
    assert result.raw_value is None


async def test_search_intent_unavailable_when_no_related_keywords() -> None:
    engine = make_mock_engine([result_with_all([])])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        result = await pytrends_indicators.compute_search_intent_score(["air fryer"], "ES", "2026-07", language="en")

    assert result.availability_status == AvailabilityStatus.UNAVAILABLE
    assert result.raw_value is None


async def test_search_intent_unavailable_when_total_weight_zero() -> None:
    # Related keywords present but all volumes are 0 -> filtered out -> UNAVAILABLE.
    rows = [("buy iberico ham", 0.0, None, None, None), ("what is iberico ham", 0.0, None, None, None)]
    engine = make_mock_engine([result_with_all(rows), result_with_all([])])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        result = await pytrends_indicators.compute_search_intent_score(["iberico ham"], "ES", "2026-07", language="en")

    assert result.availability_status == AvailabilityStatus.UNAVAILABLE
    assert result.raw_value is None


# --- Growth/Stability leem o MESMO conjunto que o Demand Volume ------------


async def test_growth_uses_the_related_expansion_like_demand_volume() -> None:
    """A falha em produção (análise e152761b, `3D printer filament`): os termos
    CURADOS não tinham volume em BRA/PRT/ESP e eram descartados por
    `_dedup_volume_rows`, deixando a série mensal vazia. Growth e Stability
    voltavam UNAVAILABLE e derrubavam a análise inteira pelo portão de
    completude — com o Demand Volume dos MESMOS países já calculado, porque só
    ele dobrava as keywords relacionadas.

    Não é imputação: é ler o dado que o sistema já coletou e no qual já confia
    para definir a demanda do produto.
    """
    # Termo curado presente mas com volume ~zero: `_dedup_volume_rows` o descarta
    # e a série mensal fica vazia — foi o que aconteceu em produção.
    aprovados = [("filamento 3d", 0.0, _months([0.0] * 12))]
    relacionados = [("filamento 3d pla", 4800.0, _months([400.0] * 6 + [600.0] * 6), 0.9, 100)]
    engine = make_mock_engine([result_with_all(aprovados), result_with_all(relacionados)])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        resultado = await pytrends_indicators.compute_growth_score(
            ["filamento 3d"], "Brazil", "2026-08", seed_terms=["filamento 3d"], language="pt"
        )

    assert resultado.availability_status == AvailabilityStatus.AVAILABLE
    assert resultado.raw_value is not None


async def test_stability_uses_the_related_expansion_like_demand_volume() -> None:
    aprovados = [("filamento 3d", 0.0, _months([0.0] * 12))]
    relacionados = [("filamento 3d pla", 4800.0, _months([400.0] * 12), 0.9, 100)]
    engine = make_mock_engine([result_with_all(aprovados), result_with_all(relacionados)])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        resultado = await pytrends_indicators.compute_demand_stability_score(
            ["filamento 3d"], "Brazil", "2026-08", seed_terms=["filamento 3d"], language="pt"
        )

    assert resultado.availability_status == AvailabilityStatus.AVAILABLE
    assert resultado.raw_value == 0.0, "série constante = CV zero"


async def test_sem_dado_nenhum_continua_indisponivel() -> None:
    """Resiliência não é inventar dado. Se não há volume em lugar nenhum — nem
    curado nem relacionado — o indicador segue UNAVAILABLE e o país é excluído
    com motivo, que é a regra do Cap. 5 §5.3."""
    engine = make_mock_engine([result_with_all([]), result_with_all([])])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        resultado = await pytrends_indicators.compute_growth_score(
            ["termo sem volume"], "Brazil", "2026-08", seed_terms=["termo sem volume"], language="pt"
        )

    assert resultado.availability_status == AvailabilityStatus.UNAVAILABLE


# --- VETO CRUZADO DE MARCAS -------------------------------------------------


async def test_demanda_nao_soma_keyword_de_outra_marca() -> None:
    """As keywords relacionadas que o Google devolve para os termos de uma marca
    incluem as concorrentes — comparativos, listas, substituições. O filtro de
    relevância não as pega quando a keyword carrega ao mesmo tempo um termo
    aprovado E outra marca, e aí o volume de uma marca entra na demanda da
    outra. Medido no armazém real (Cardhu, GBR): os oito termos aprovados
    carregavam todos "cardhu", então as 21 relacionadas sem a marca caíram pelo
    filtro de relevância — sorte, não garantia."""
    approved = [("cardhu 12", 1000.0, _months([100.0] * 12))]
    related = [
        ("cardhu 12 price", 2000.0, _months([200.0] * 12), 1.2, 100),  # a marca -> entra
        ("glenfiddich 12 vs cardhu 12", 9000.0, _months([900.0] * 12), 1.5, 90),  # outra marca -> vetada
        ("macallan 12 price", 8000.0, _months([800.0] * 12), 1.4, 95),  # outra marca -> vetada
    ]
    engine = make_mock_engine([result_with_all(approved), result_with_all(related)])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        result = await pytrends_indicators.compute_trend_score(
            ["cardhu 12"], "GB", "2026-08", language="en", outras_marcas=["glenfiddich", "macallan"]
        )

    # 1000 (aprovado) + 2000 (a própria marca). As 17.000 das concorrentes ficam fora.
    assert result.raw_value == pytest.approx(3000.0)
    assert result.notes is not None and "+1 folded" in result.notes


async def test_sem_marca_a_categoria_soma_as_marcas() -> None:
    """Numa entidade genérica — a categoria — consulta de marca é demanda
    legítima: quem procura "cardhu" está no mercado de whisky. O veto não age,
    e é por isso que ele é uma lista vinda de fora e não uma regra fixa."""
    approved = [("single malt whisky", 1000.0, _months([100.0] * 12))]
    related = [("cardhu single malt whisky", 2000.0, _months([200.0] * 12), 1.2, 100)]
    engine = make_mock_engine([result_with_all(approved), result_with_all(related)])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        result = await pytrends_indicators.compute_trend_score(
            ["single malt whisky"], "GB", "2026-08", language="en", outras_marcas=[]
        )

    assert result.raw_value == pytest.approx(3000.0)


async def test_intencao_de_compra_ignora_keyword_de_outra_marca() -> None:
    """A intenção é média das relacionadas ponderada por volume, então um
    comparativo de alto volume da concorrente pesaria na intenção deste
    produto. Aqui a keyword vetada é transacional e a da marca é de curiosidade:
    se o veto não agisse, o score subiria."""
    related_com_veto = [
        ("what is cardhu", 100.0, _months([10.0] * 12), 0.1, 5),
        ("buy glenfiddich 12 online", 10_000.0, _months([1000.0] * 12), 2.0, 100),
    ]
    engine = make_mock_engine([result_with_all(related_com_veto)])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        vetado = await pytrends_indicators.compute_search_intent_score(
            ["cardhu"], "GB", "2026-08", "en", outras_marcas=["glenfiddich"]
        )

    engine = make_mock_engine([result_with_all(related_com_veto)])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        sem_veto = await pytrends_indicators.compute_search_intent_score(["cardhu"], "GB", "2026-08", "en")

    assert vetado.raw_value is not None and sem_veto.raw_value is not None
    assert float(vetado.raw_value) < float(sem_veto.raw_value), "a concorrente inflava a intenção deste produto"


def test_veto_compara_forma_normalizada() -> None:
    """A comparação é sobre a forma normalizada, então grafia e pontuação não
    escapam do veto. Limite conhecido e aceito: marca transliterada (`カーデュ`)
    não é reconhecida — o normalizador remove tudo fora de [a-z0-9]."""
    assert pytrends_indicators._de_outra_marca("Glenfiddich-12 Review", ["glenfiddich"])
    assert pytrends_indicators._de_outra_marca("GLEN FIDDICH 12", ["glenfiddich"])
    assert not pytrends_indicators._de_outra_marca("cardhu 12", ["glenfiddich"])
    assert not pytrends_indicators._de_outra_marca("cardhu 12", [])
    assert not pytrends_indicators._de_outra_marca("cardhu 12", None)


# --- SEEDS COMPARTILHADOS ENTRE MARCA E CATEGORIA ---------------------------


def test_seeds_divididos_14_6_entre_marca_e_categoria() -> None:
    """`keywords_for_keywords` aceita 20 seeds por task, e numa análise com marca
    esses 20 atendem dois assuntos. 14 para a marca, que é o lado pontuado, 6
    para a categoria, que é contexto — divisão fixa porque previsível é
    auditável."""
    marca = [f"marca {i}" for i in range(30)]
    categoria = [f"categoria {i}" for i in range(30)]
    seeds = pytrends_indicators.build_seed_terms(marca, categoria)

    assert len(seeds) == 20
    assert seeds[:14] == marca[:14]
    assert seeds[14:] == categoria[:6]


def test_sobra_de_um_lado_vai_para_o_outro() -> None:
    """Lado curto não desperdiça slot: a request é a mesma e o custo é o mesmo,
    então os 20 são sempre usados quando há termo disponível."""
    seeds = pytrends_indicators.build_seed_terms(["m1", "m2"], [f"c{i}" for i in range(30)])
    assert len(seeds) == 20
    assert seeds[:2] == ["m1", "m2"]

    # Sem categoria, a marca usa os 20 — o comportamento de sempre.
    assert pytrends_indicators.build_seed_terms([f"m{i}" for i in range(30)]) == [f"m{i}" for i in range(20)]
    # Sem marca (entidade genérica), a categoria usa os 20.
    assert pytrends_indicators.build_seed_terms([], ["c1", "c2"]) == ["c1", "c2"]


def test_termo_repetido_nos_dois_lados_conta_uma_vez() -> None:
    """Um slot gasto duas vezes com a mesma palavra é um slot perdido."""
    assert pytrends_indicators.build_seed_terms(["air fryer"], ["Air Fryer"]) == ["air fryer"]


async def test_intencao_da_marca_ignora_related_da_categoria() -> None:
    """Com os seeds compartilhados, as related voltam TODAS sob o mesmo rótulo —
    então a intenção de compra da marca seria composta também pelas keywords
    genéricas da categoria. O portão de relevância só liga nesse caso: com um
    assunto só, exigir que a keyword contenha o nome do produto descartaria
    keyword local legítima que não o repete."""
    related = [
        ("cardhu 12 where to buy", 100.0, _months([10.0] * 12), 2.0, 100),  # da marca
        ("what is single malt whisky", 20_000.0, _months([2000.0] * 12), 0.1, 5),  # da categoria
    ]
    engine = make_mock_engine([result_with_all(related)])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        com_portao = await pytrends_indicators.compute_search_intent_score(
            ["cardhu 12", "single malt whisky"], "GB", "2026-08", "en", relevant_terms=["cardhu 12"]
        )

    engine = make_mock_engine([result_with_all(related)])
    with patch("app.services.indicators.pytrends.get_external_engine", return_value=engine):
        sem_portao = await pytrends_indicators.compute_search_intent_score(
            ["cardhu 12", "single malt whisky"], "GB", "2026-08", "en"
        )

    assert com_portao.raw_value is not None and sem_portao.raw_value is not None
    # A keyword genérica é de curiosidade e tem 200x o volume: sem o portão ela
    # domina a média ponderada e afunda a intenção da marca.
    assert float(com_portao.raw_value) > float(sem_portao.raw_value)
