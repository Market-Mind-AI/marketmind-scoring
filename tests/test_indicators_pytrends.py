"""Demand raw-value functions, now sourced from Google Ads ABSOLUTE search
volume (DataForSEO) instead of Google Trends (2026-07-15). The four signals
read the collector's own `keyword_search_volume` / `keyword_related` tables:
LEVEL = ln(SUM of the country's terms' volume); GROWTH/STABILITY from the summed
monthly series; SEARCH INTENT from volume-weighted related-keyword classification.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.domain.enums import AvailabilityStatus, IndicatorDirection
from app.services.indicators import pytrends as pytrends_indicators
from tests._source_mock import patch_pytrends


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
    with patch_pytrends(rows, []):
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
    with patch_pytrends(rows, []):
        result = await pytrends_indicators.compute_trend_score(["air fryer", "airfryer"], "ES", "2026-07")

    assert result.availability_status == AvailabilityStatus.AVAILABLE
    # Only 1000 counted once: 1000, not 2000.
    assert result.raw_value == pytest.approx(1000.0)


async def test_trend_score_expands_with_the_product_and_honours_the_curation_veto() -> None:
    """The expansion folds high-volume related keywords that are clearly the
    product, and drops what the curation ruled out for this market. Which words
    are not purchase intent is the curation's answer, not a list kept here: the
    same model call that finds the country's term returns `excluded_terms`, and
    it knows that "recipe" is someone who already owns one."""
    approved = [("air fryer", 1000.0, _months([100.0] * 12))]
    related = [  # (keyword, volume, monthly, cpc, competition_index)
        ("air fryer ninja", 3000.0, _months([300.0] * 12), 1.2, 100),  # the product -> folded
        ("air fryer recipe", 9000.0, _months([900.0] * 12), 0.1, 20),  # vetoed by the curation
        ("what is an air fryer", 5000.0, _months([500.0] * 12), 0.1, 5),  # vetoed by the curation
        ("blender", 8000.0, _months([800.0] * 12), 1.0, 100),  # not the product -> dropped
    ]
    with patch_pytrends(approved, related):
        result = await pytrends_indicators.compute_trend_score(
            ["air fryer"],
            "US",
            "2026-07",
            language="en",
            excluded_terms=["air fryer recipe", "what is an air fryer"],
        )

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
    with patch_pytrends(approved, related):
        result = await pytrends_indicators.compute_trend_score(["Airfryer"], "DE", "2026-07", language="de")

    assert result.availability_status == AvailabilityStatus.AVAILABLE
    # 301000 counted ONCE + 50000, not 301000 x3 + 50000.
    assert result.raw_value == pytest.approx(351000.0)


async def test_trend_score_accepts_json_string_monthly_searches() -> None:
    # monthly_searches may arrive as a JSON string (raw JSONB) — parsed the same.
    rows = [("air fryer", 200.0, json.dumps(_months([20.0] * 12)))]
    with patch_pytrends(rows, []):
        result = await pytrends_indicators.compute_trend_score(["air fryer"], "ES", "2026-07")

    assert result.availability_status == AvailabilityStatus.AVAILABLE
    assert result.raw_value == pytest.approx(200.0)


async def test_trend_score_unavailable_when_no_rows() -> None:
    with patch_pytrends([], []):
        result = await pytrends_indicators.compute_trend_score(["air fryer"], "ES", "2026-07")

    assert result.availability_status == AvailabilityStatus.UNAVAILABLE
    assert result.raw_value is None


# --- GROWTH (compute_growth_score) -----------------------------------------


async def test_growth_score_computes_expected_raw() -> None:
    # One term's monthly series [10,10,10,20,20,20,40,40,40]: n=9, k=3,
    # initial_avg=10, final_avg=40 -> ((40-10)/10)*100 = 300.
    rows = [("air fryer", 200.0, _months([10, 10, 10, 20, 20, 20, 40, 40, 40]))]
    with patch_pytrends(rows, []):
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
    with patch_pytrends(rows, []):
        result = await pytrends_indicators.compute_growth_score(["air fryer", "freidora de aire"], "ES", "2026-07")

    assert result.availability_status == AvailabilityStatus.AVAILABLE
    assert result.raw_value == pytest.approx(150.0)


async def test_growth_score_unavailable_with_fewer_than_six_months() -> None:
    rows = [("air fryer", 60.0, _months([10, 20, 30]))]
    with patch_pytrends(rows, []):
        result = await pytrends_indicators.compute_growth_score(["air fryer"], "ES", "2026-07")

    assert result.availability_status == AvailabilityStatus.UNAVAILABLE
    assert result.raw_value is None


async def test_growth_score_flat_zero_on_near_zero_baseline() -> None:
    # Low-base noise guard: a near-zero initial window makes the ratio explode.
    # Must return a flat 0% (AVAILABLE), not a spurious extreme nor UNAVAILABLE.
    rows = [("air fryer", 153.0, _months([0, 0, 1, 0, 0, 2, 40, 50, 60]))]
    with patch_pytrends(rows, []):
        result = await pytrends_indicators.compute_growth_score(["air fryer"], "ES", "2026-07")

    assert result.availability_status == AvailabilityStatus.AVAILABLE
    assert result.raw_value == 0.0


# --- DEMAND STABILITY (compute_demand_stability_score) ----------------------


async def test_demand_stability_score_computes_cv() -> None:
    rows = [("air fryer", 60.0, _months([10, 20, 30]))]
    with patch_pytrends(rows, []):
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
    with patch_pytrends(rows, []):
        result = await pytrends_indicators.compute_demand_stability_score(["presunto pata negra"], "PT", "2026-07")

    assert result.availability_status == AvailabilityStatus.AVAILABLE
    assert result.raw_value == pytest.approx(0.5)  # neutral CV
    assert result.notes is not None and "low-volume" in result.notes


async def test_demand_stability_unavailable_with_fewer_than_two_months() -> None:
    rows = [("air fryer", 10.0, _months([10]))]
    with patch_pytrends(rows, []):
        result = await pytrends_indicators.compute_demand_stability_score(["air fryer"], "ES", "2026-07")

    assert result.availability_status == AvailabilityStatus.UNAVAILABLE
    assert result.raw_value is None


# --- SEARCH INTENT (compute_search_intent_score) ---
#
# There is no word list here any more, in any language. Purchase intent is read
# from what advertisers pay for the query: competition index and CPC. Nobody
# bids on a search that does not lead to a sale, and how hard they bid says how
# much it leads to. The word list this replaced knew "como cozinhar" and missed
# "picanha na airfryer" (9,900/month) — the form people actually type — and
# keeping it right would have meant maintaining every language times every
# product category by hand.


async def test_search_intent_weights_purchase_value_by_volume() -> None:
    # Competition 100 on the query buyers type, 0 on the one they don't, and the
    # volume behind each decides the weight: (80*1.0 + 20*0.0) / 100 * 100.
    rows = [
        ("comprar picanha", 80.0, None, None, 100),
        ("picanha na airfryer", 20.0, None, None, 0),
    ]
    with patch_pytrends([], rows):
        result = await pytrends_indicators.compute_search_intent_score(["picanha"], "BR", "2026-07")

    assert result.availability_status == AvailabilityStatus.AVAILABLE
    assert result.raw_value == pytest.approx(80.0)


async def test_search_intent_reads_cpc_when_competition_is_missing() -> None:
    # CPC is normalized inside the country's own related set — never across
    # countries, or the richer market would look more intentional rather than
    # more expensive. 2.00 is the local top (1.0); 0.50 is a quarter of it.
    rows = [("comprar picanha", 50.0, None, 2.0, None), ("picanha preco", 50.0, None, 0.5, None)]
    with patch_pytrends([], rows):
        result = await pytrends_indicators.compute_search_intent_score(["picanha"], "BR", "2026-07")

    assert result.availability_status == AvailabilityStatus.AVAILABLE
    assert result.raw_value == pytest.approx(62.5)  # (50*1.0 + 50*0.25) / 100 * 100


async def test_search_intent_unavailable_when_nobody_bids_on_anything() -> None:
    """Volume with no bidding data at all is reported as unavailable, not as low
    intent: a market nobody advertises in and a market we have no advertising
    data for look identical from here, and scoring the second one would be an
    invention that the ranking would then carry as a fact."""
    rows = [("picanha", 100.0, None, None, None), ("picanha na brasa", 40.0, None, None, None)]
    with patch_pytrends([], rows):
        result = await pytrends_indicators.compute_search_intent_score(["picanha"], "BR", "2026-07")

    assert result.availability_status == AvailabilityStatus.UNAVAILABLE
    assert result.raw_value is None
    assert result.notes is not None and "none with competition or CPC" in result.notes


async def test_search_intent_unavailable_when_no_seed_terms() -> None:
    # Empty seeds -> UNAVAILABLE without ever asking the collection service.
    with patch_pytrends([], []):
        result = await pytrends_indicators.compute_search_intent_score([], "ES", "2026-07")

    assert result.availability_status == AvailabilityStatus.UNAVAILABLE
    assert result.raw_value is None


async def test_search_intent_unavailable_when_no_related_keywords() -> None:
    with patch_pytrends([], []):
        result = await pytrends_indicators.compute_search_intent_score(["air fryer"], "ES", "2026-07")

    assert result.availability_status == AvailabilityStatus.UNAVAILABLE
    assert result.raw_value is None


async def test_search_intent_unavailable_when_total_weight_zero() -> None:
    # Related keywords present but all volumes are 0 -> filtered out -> UNAVAILABLE.
    rows = [("buy iberico ham", 0.0, None, None, None), ("what is iberico ham", 0.0, None, None, None)]
    with patch_pytrends([], rows):
        result = await pytrends_indicators.compute_search_intent_score(["iberico ham"], "ES", "2026-07")

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
    with patch_pytrends(aprovados, relacionados):
        resultado = await pytrends_indicators.compute_growth_score(
            ["filamento 3d"], "Brazil", "2026-08", seed_terms=["filamento 3d"], language="pt"
        )

    assert resultado.availability_status == AvailabilityStatus.AVAILABLE
    assert resultado.raw_value is not None


async def test_stability_uses_the_related_expansion_like_demand_volume() -> None:
    aprovados = [("filamento 3d", 0.0, _months([0.0] * 12))]
    relacionados = [("filamento 3d pla", 4800.0, _months([400.0] * 12), 0.9, 100)]
    with patch_pytrends(aprovados, relacionados):
        resultado = await pytrends_indicators.compute_demand_stability_score(
            ["filamento 3d"], "Brazil", "2026-08", seed_terms=["filamento 3d"], language="pt"
        )

    assert resultado.availability_status == AvailabilityStatus.AVAILABLE
    assert resultado.raw_value == 0.0, "série constante = CV zero"


async def test_sem_dado_nenhum_continua_indisponivel() -> None:
    """Resiliência não é inventar dado. Se não há volume em lugar nenhum — nem
    curado nem relacionado — o indicador segue UNAVAILABLE e o país é excluído
    com motivo, que é a regra do Cap. 5 §5.3."""
    with patch_pytrends([], []):
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
    with patch_pytrends(approved, related):
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
    with patch_pytrends(approved, related):
        result = await pytrends_indicators.compute_trend_score(
            ["single malt whisky"], "GB", "2026-08", language="en", outras_marcas=[]
        )

    assert result.raw_value == pytest.approx(3000.0)


async def test_intencao_de_compra_ignora_keyword_de_outra_marca() -> None:
    """A intenção é média das relacionadas ponderada por volume, então um
    comparativo de alto volume da concorrente pesaria na intenção deste
    produto. Aqui a keyword vetada é aquela em que os anunciantes brigam e a da
    marca é a que ninguém disputa: se o veto não agisse, o score subiria."""
    related_com_veto = [
        ("what is cardhu", 100.0, _months([10.0] * 12), 0.1, 5),
        ("buy glenfiddich 12 online", 10_000.0, _months([1000.0] * 12), 2.0, 100),
    ]
    with patch_pytrends([], related_com_veto):
        vetado = await pytrends_indicators.compute_search_intent_score(
            ["cardhu"], "GB", "2026-08", outras_marcas=["glenfiddich"]
        )

    with patch_pytrends([], related_com_veto):
        sem_veto = await pytrends_indicators.compute_search_intent_score(["cardhu"], "GB", "2026-08")

    assert vetado.raw_value is not None and sem_veto.raw_value is not None
    assert float(vetado.raw_value) < float(sem_veto.raw_value), "a concorrente inflava a intenção deste produto"


def test_veto_compara_forma_normalizada() -> None:
    """A comparação é sobre a forma normalizada, então grafia e pontuação não
    escapam do veto. Limite conhecido e aceito: marca transliterada (`カーデュ`)
    não é reconhecida — é outra palavra, não outra grafia."""
    assert pytrends_indicators._de_outra_marca("Glenfiddich-12 Review", ["glenfiddich"])
    assert pytrends_indicators._de_outra_marca("GLEN FIDDICH 12", ["glenfiddich"])
    assert not pytrends_indicators._de_outra_marca("cardhu 12", ["glenfiddich"])
    assert not pytrends_indicators._de_outra_marca("cardhu 12", [])
    assert not pytrends_indicators._de_outra_marca("cardhu 12", None)


def test_veto_reconhece_marca_de_duas_palavras_e_escrita_nao_latina() -> None:
    """As marcas são gravadas com `normalize_alias`, com espaço. Até 26/10/2026
    a keyword era comparada sem espaço e só em ASCII, então 'juan valdez' nunca
    estava em 'juanvaldezcafe' e nenhuma marca em japonês era vetada."""
    assert pytrends_indicators._de_outra_marca("Juan Valdez café 500g", ["juan valdez"])
    assert pytrends_indicators._de_outra_marca("ネスカフェ ゴールドブレンド", ["ネスカフェ"])


def test_termo_nao_latino_tem_chave_e_expande() -> None:
    """Um termo em japonês normalizava para string vazia, e o portão de
    relevância da expansão não tinha contra o que comparar: o país perdia
    todas as palavras relacionadas."""
    related = [{"keyword": "ノンフライヤー おすすめ", "volume": 900.0, "monthly": []}]
    escolhidas = pytrends_indicators._select_expansion(related, ["ノンフライヤー"])
    assert [k for k, _v, _m in escolhidas] == ["ノンフライヤー おすすめ"]


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
    with patch_pytrends([], related):
        com_portao = await pytrends_indicators.compute_search_intent_score(
            ["cardhu 12", "single malt whisky"], "GB", "2026-08", relevant_terms=["cardhu 12"]
        )

    with patch_pytrends([], related):
        sem_portao = await pytrends_indicators.compute_search_intent_score(
            ["cardhu 12", "single malt whisky"], "GB", "2026-08"
        )

    assert com_portao.raw_value is not None and sem_portao.raw_value is not None
    # A keyword genérica não tem lance de anunciante e tem 200x o volume: sem o
    # portão ela domina a média ponderada e afunda a intenção da marca.
    assert float(com_portao.raw_value) > float(sem_portao.raw_value)


async def test_categoria_sem_expansao_soma_apenas_os_termos_curados() -> None:
    """A categoria é o denominador que responde "existe mercado para este tipo
    de produto aqui?". A expansão dobraria keywords de concorrentes — medido ao
    vivo, a categoria da Espanha somou `grant's scotch whisky` (720) porque ela
    contém o termo aprovado "scotch whisky" — e o veto por cadastro não pega
    marca que a plataforma nunca viu. Sem expansão, o número é exatamente o que
    o rótulo promete."""
    approved = [("scotch whisky", 590.0, _months([50.0] * 12))]
    related = [("grant's scotch whisky", 720.0, _months([60.0] * 12), 1.5, 90)]
    with patch_pytrends(approved, related):
        sem_expansao = await pytrends_indicators.compute_trend_score(
            ["scotch whisky"], "ES", "2026-08", language="es", expandir=False
        )
    assert sem_expansao.raw_value == pytest.approx(590.0)
    assert "folded" not in (sem_expansao.notes or "")

    # Com expansão (o caminho da MARCA), a concorrente desconhecida entraria.
    with patch_pytrends(approved, related):
        com_expansao = await pytrends_indicators.compute_trend_score(["scotch whisky"], "ES", "2026-08", language="es")
    assert float(com_expansao.raw_value) > 590.0


# --- Google's close-variant groups are one search, not many ---


def _rows(*pairs: tuple[str, float]) -> list:
    return [(k, v, _months([v / 12] * 12)) for k, v in pairs]


def _vocabulary(*search_terms: str) -> frozenset[str]:
    """The words the curation asked for. Every related keyword carries one by
    construction, so they are the words that prove nothing — see
    _dedup_volume_rows. Passed here because production passes it; with an empty
    set these tests would all pass while the real path was broken."""
    from app.utils.text import query_words

    return frozenset().union(*(query_words(t) for t in search_terms))


def test_the_same_search_written_many_ways_counts_once() -> None:
    """Real data, picanha in the US. Google returned one search under four
    spellings, all at exactly 90,500 — summed, that is 271,500 searches that
    do not exist."""
    from app.services.indicators.pytrends import _dedup_volume_rows

    out = _dedup_volume_rows(
        _rows(
            ("picanha steak", 90_500),
            ("picaña steak", 90_500),
            ("picanha beef steak", 90_500),
            ("beef picanha steak", 90_500),
        ),
        _vocabulary("picanha steak"),
    )

    assert len(out) == 1
    assert sum(v for _k, v, _m in out) == 90_500


def test_a_nine_spelling_group_counts_once() -> None:
    """The coulotte group in the same response. The old rule compared how alike
    the strings looked, so "coulotte beef" and "coulotte steak" stayed apart
    and 18,100 became 162,900."""
    from app.services.indicators.pytrends import _dedup_volume_rows

    out = _dedup_volume_rows(
        _rows(
            ("coulotte beef", 18_100),
            ("coulotte steak", 18_100),
            ("coulotte sirloin", 18_100),
            ("top sirloin coulotte", 18_100),
            ("sirloin coulotte", 18_100),
            ("sirloin coulotte steak", 18_100),
            ("steak coulotte", 18_100),
            ("beef coulotte", 18_100),
            ("beef coulotte steak", 18_100),
        ),
        _vocabulary("picanha steak", "beef picanha"),
    )

    assert sum(v for _k, v, _m in out) == 18_100


def test_brands_at_the_same_volume_are_different_products() -> None:
    """Real data, picanha in Brazil. Google reported "picanha swift", "picanha
    bassi", "picanha wagyu" and "picanha angus" at exactly 3,600 each — four
    products from four sellers, and the two brands people actually buy. The rule
    that stood here was "same volume plus any shared word", and the word they
    share is "picanha", which the relevance gate guarantees every related
    keyword carries. So all four were read as one search."""
    from app.services.indicators.pytrends import _dedup_volume_rows

    out = _dedup_volume_rows(
        _rows(
            ("picanha swift", 3_600),
            ("picanha bassi", 3_600),
            ("picanha wagyu", 3_600),
            ("picanha angus", 3_600),
        ),
        _vocabulary("picanha", "picanha bovina", "carne picanha"),
    )

    assert len(out) == 4
    assert sum(v for _k, v, _m in out) == 14_400


def test_the_same_brand_written_two_ways_still_counts_once() -> None:
    """The other half of the same real group: "bassi picanha", "picanha bassi"
    and "picanha do bassi" are one search. Word order and filler words are not a
    different query."""
    from app.services.indicators.pytrends import _dedup_volume_rows

    out = _dedup_volume_rows(
        _rows(("picanha bassi", 3_600), ("bassi picanha", 3_600), ("picanha do bassi", 3_600)),
        _vocabulary("picanha"),
    )

    assert sum(v for _k, v, _m in out) == 3_600


def test_two_ways_of_preparing_it_are_two_searches() -> None:
    """Also real, also at 8,100: "picanha na brasa" and "picanha preço" share
    only the product's name, so they are two searches and both are counted."""
    from app.services.indicators.pytrends import _dedup_volume_rows

    out = _dedup_volume_rows(_rows(("picanha na brasa", 8_100), ("picanha preço", 8_100)), _vocabulary("picanha"))

    assert sum(v for _k, v, _m in out) == 16_200


def test_different_volumes_are_different_searches_and_are_summed() -> None:
    """The check that keeps this from eating real demand: 'air fryer' at 550k
    and 'airfryer' at 74k are two searches, however alike they read."""
    from app.services.indicators.pytrends import _dedup_volume_rows

    out = _dedup_volume_rows(_rows(("air fryer", 550_000), ("airfryer", 74_000)), _vocabulary("air fryer"))

    assert sum(v for _k, v, _m in out) == 624_000


def test_an_identical_volume_with_nothing_in_common_stays_separate() -> None:
    """Google rounds to coarse buckets, so two unrelated searches can land in
    the same one. Sharing a bucket is not evidence on its own."""
    from app.services.indicators.pytrends import _dedup_volume_rows

    out = _dedup_volume_rows(_rows(("picanha", 18_100), ("wagyu brisket", 18_100)), _vocabulary("picanha"))

    assert sum(v for _k, v, _m in out) == 36_200


def test_the_shortest_spelling_is_the_one_reported() -> None:
    """The per-term breakdown has to read as words a person recognises."""
    from app.services.indicators.pytrends import _dedup_volume_rows

    out = _dedup_volume_rows(
        _rows(("beef picanha steak roast", 90_500), ("picanha steak", 90_500)), _vocabulary("picanha steak")
    )

    assert [k for k, _v, _m in out] == ["picanha steak"]


# --- the curation's veto list applies to the expansion too ---


def _related(*pairs: tuple[str, float]) -> list:
    return [{"keyword": k, "volume": v, "monthly": [], "cpc": None, "competition_index": None} for k, v in pairs]


def test_a_term_the_curation_ruled_out_does_not_return_through_the_expansion() -> None:
    """Real data, picanha in Brazil. "picanha suína" is pork — a different cut
    of a different animal — and the curation listed it as excluded. It came
    back anyway at 14,800 searches a month, because it contains an approved
    term and the expansion never consulted the veto list."""
    from app.services.indicators.pytrends import _select_expansion

    picked = _select_expansion(
        _related(("picanha suína", 14_800), ("picanha suina", 14_800), ("preço da picanha", 8_100)),
        ["picanha"],
        None,
        ["picanha suína", "picanha de porco"],
    )

    assert [k for k, _v, _m in picked] == ["preço da picanha"]


def test_the_veto_ignores_accents_and_case() -> None:
    """The curation writes "picanha suína"; Google returns "PICANHA SUINA"."""
    from app.services.indicators.pytrends import _select_expansion

    picked = _select_expansion(_related(("PICANHA SUINA", 14_800)), ["picanha"], None, ["picanha suína"])

    assert picked == []


def test_with_nothing_vetoed_the_expansion_is_unchanged() -> None:
    from app.services.indicators.pytrends import _select_expansion

    picked = _select_expansion(_related(("preço da picanha", 8_100)), ["picanha"], None, [])

    assert [k for k, _v, _m in picked] == ["preço da picanha"]


# --- what a term MEANS is curated; what it is WORTH comes from the market ---
#
# The three tests that stood here checked a per-language word list against
# Portuguese cooking phrases. The list is gone: it had to be right for every
# language times every product category, and it was wrong in exactly the way
# that predicts — it knew "como cozinhar" and missed "na airfryer", which is
# how people actually write it, so 9,900 searches a month from people who
# already owned the meat counted as demand to buy it.
#
# Deciding that "na airfryer" is not demand is now the curation's, one model
# call per product per market, and it is asserted through the excluded-terms
# veto above. Scoring how much purchase value a market's demand carries is now
# advertiser competition and CPC — what someone pays to reach that query —
# which needs no maintenance and knows no language.


async def test_the_curation_veto_reaches_every_demand_signal() -> None:
    """The four demand signals have to read the SAME set of terms — the engine
    depends on it. The veto used to be wired into the volume only, so a keyword
    the curation ruled out still shaped growth, stability and intent: "picanha
    suína" is pork, and pork's seasonality and pork's advertisers were being
    read as this product's."""
    approved = [("picanha", 100_000.0, _months([8_000.0] * 12))]
    pork = ("picanha suina", 50_000.0, _months([1_000.0 * m for m in range(1, 13)]), 3.0, 100)
    related = [pork]

    with patch_pytrends(approved, related):
        volume = await pytrends_indicators.compute_trend_score(
            ["picanha"], "BR", "2026-07", excluded_terms=["picanha suína"]
        )
    with patch_pytrends(approved, related):
        growth = await pytrends_indicators.compute_growth_score(
            ["picanha"], "BR", "2026-07", excluded_terms=["picanha suína"]
        )
    with patch_pytrends(approved, related):
        stability = await pytrends_indicators.compute_demand_stability_score(
            ["picanha"], "BR", "2026-07", excluded_terms=["picanha suína"]
        )
    with patch_pytrends([], related):
        intent = await pytrends_indicators.compute_search_intent_score(
            ["picanha"], "BR", "2026-07", excluded_terms=["picanha suína"]
        )

    # Only the approved term survives: its volume, its flat series (growth 0,
    # CV 0), and no bidding data left to score intent with.
    assert volume.raw_value == pytest.approx(100_000.0)
    assert growth.raw_value == pytest.approx(0.0)
    assert stability.raw_value == pytest.approx(0.0)
    assert intent.availability_status == AvailabilityStatus.UNAVAILABLE


def test_the_expansion_keeps_everything_that_qualifies() -> None:
    """There is no top-N. There was one — the 15 highest-volume related keywords
    — and on real data it was deciding the answer rather than trimming noise:
    Google returned 2,127 related keywords for picanha in Brazil, 1,967 passed
    every gate, and the cap kept 15. "picanha swift" ranked 22nd. A cap on a SUM
    is a cap on the measurement, and it bit hardest in the market where Google
    knows the vocabulary best, which is backwards."""
    from app.services.indicators.pytrends import _select_expansion

    related = _related(*[(f"picanha {n}", float(5000 - n)) for n in range(40)])

    picked = _select_expansion(related, ["picanha"], None, [])

    assert len(picked) == 40


def test_grouping_two_thousand_keywords_stays_quick() -> None:
    """The only CPU-bound stretch in this service, and it runs three times per
    country. It was O(n squared) over SequenceMatcher, which was fine while the
    expansion was capped at fifteen keywords and stopped being fine the moment
    the cap came off — 1.1 seconds of blocked event loop per call, on a
    container limited to one CPU, which is how Kubernetes ended up restarting
    the pod mid-analysis.

    Two changes keep it quick and neither moves a single row: grouping by the
    reported volume first (rows at different volumes could never merge anyway),
    and using SequenceMatcher's own cheap upper bounds before the real ratio.
    """
    import time

    from app.services.indicators.pytrends import _dedup_volume_rows

    # Shaped like the real thing: many keywords sharing the product's name, a
    # handful of coarse volume buckets, so the buckets are genuinely large.
    rows = _rows(*[(f"picanha {n} {'wagyu' if n % 3 else 'angus'}", 100.0 * (n % 12 + 1)) for n in range(2000)])

    started = time.perf_counter()
    out = _dedup_volume_rows(rows, _vocabulary("picanha"))
    elapsed = time.perf_counter() - started

    assert out, "sanity: the grouping produced something"
    assert elapsed < 1.0, f"grouping took {elapsed:.2f}s; it used to block the event loop for longer than this"


# --- Vencimento da demanda (23/09/2026) -------------------------------------
#
# `related_rows_exist` não tinha teste nenhum: é mockado no único lugar que o
# usa, então a suíte inteira passava verde sem nunca executá-lo. É a mesma
# forma de cegueira que deixou seis defeitos viverem semanas neste projeto —
# o teste mede o que o código faz, não o ambiente em que ele roda.


def _relacionada(volume: int, *, dias: int) -> dict:
    """Uma linha como o serviço devolve: `collected_at` COM fuso, porque a
    coluna é `timestamp with time zone`."""
    return {
        "keyword": "air fryer barata",
        "search_volume": volume,
        "collected_at": (datetime.now(UTC) - timedelta(days=dias)).isoformat(),
    }


async def test_related_keywords_collected_long_ago_count_as_absent() -> None:
    """Relacionadas são demanda, e demanda muda de mês para mês. Sem idade, um
    conjunto de sementes estável mantinha para sempre as relacionadas da
    primeira coleta — e o intent score sai delas. A janela é de um semestre."""
    with patch(
        "app.services.indicators.pytrends.pytrends_stored_related",
        AsyncMock(return_value=[_relacionada(900, dias=200)]),
    ):
        assert await pytrends_indicators.related_rows_exist(["air fryer"], "ES") is False


async def test_recent_related_keywords_are_not_recollected() -> None:
    """O outro lado, que é o que custa: dentro do prazo não se pergunta à
    DataForSEO, que cobra por chamada. 45 dias ainda é dentro: em 29/10/2026
    a air fryer pagou de novo tudo o que tinha coletado entre 21 e 28/09."""
    with patch(
        "app.services.indicators.pytrends.pytrends_stored_related",
        AsyncMock(return_value=[_relacionada(900, dias=45)]),
    ):
        assert await pytrends_indicators.related_rows_exist(["air fryer"], "ES") is True


async def test_a_row_with_no_collection_date_is_treated_as_stale() -> None:
    """O lado seguro do erro: recoletar custa uma chamada, usar para sempre um
    dado de data desconhecida custa uma análise errada."""
    with patch(
        "app.services.indicators.pytrends.pytrends_stored_related",
        AsyncMock(return_value=[{"keyword": "x", "search_volume": 900}]),
    ):
        assert await pytrends_indicators.related_rows_exist(["air fryer"], "ES") is False
