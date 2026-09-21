"""Cap. 4 §4.2 — raw value computation for the Demand indicators, now sourced
from Google Ads keyword data (ABSOLUTE monthly search volume via DataForSEO),
NOT Google Trends. Reads read-only from the collector service's own database
over its read API — the stored search-volume and
`keyword_related` tables.

Why the switch (2026-07-15): Google Trends is structurally RELATIVE (values
are normalized per query / per region and only comparable across countries for
a single shared term). Absolute search volume is a real count, directly
comparable across countries with each market's OWN terms, no shared "worldwide"
term and no normalization tricks. See marketmind_mos_engine.

The four Demand signals, all per country, from the country's approved terms
(product_catalog.localized_markets[ISO3]):
  - VOLUME (trend_score): SUM of the absolute monthly search volume of the
    country's `search_terms` (deduped) = market potential / sales headroom.
    Raw = the absolute count, normalized LINEARLY (proportional to volume,
    2026-07-16 redesign — replaced the earlier ln transform). Drives the
    top-ranked Demand Volume dimension.
  - GROWTH: trend of the SUMMED monthly_searches series (trailing 12 months).
  - DEMAND STABILITY: CV of that same summed monthly series.
  - SEARCH INTENT: related keywords (`keyword_related`) classified buy-vs-
    curiosity, weighted by their real volume.
Growth/Stability/Intent form the Demand Quality dimension (size-independent).
"""

import json
import re
import unicodedata
from collections.abc import Iterable
from difflib import SequenceMatcher
from typing import Any

from app.domain.enums import AvailabilityStatus, IndicatorDirection
from app.services.indicators.intent_lexicon import INTENT_LEXICON
from app.services.indicators.results import RawIndicatorResult
from app.services.indicators.sources import pytrends_stored_related, pytrends_stored_volume

# Absolute-volume noise guards (Google Ads monthly counts, not the old 0-100
# Trends scale): below these a growth ratio / CV is noise, not signal.
_MIN_GROWTH_BASELINE = 10.0
_MIN_STABILITY_BASELINE = 10.0
_NEUTRAL_STABILITY_CV = 0.5

# Demand-term hygiene (Cap. 4 §4.2.1, 2026-07-24). Below this monthly volume a
# term is "~zero" — noise the LLM proposed that nobody actually searches; drop
# it so it neither inflates the count nor skews intent.
_VOLUME_FLOOR = 10.0
# Conservative expansion: fold at most this many high-volume RELATED keywords
# (the LLM proposes seeds; Google's related keywords confirm/expand) into the
# demand sum, capped so a data-rich market isn't disproportionately inflated.
_EXPANSION_TOP_N = 15
# Two strings at the SAME reported volume this similar are Google's close-variant
# grouping of ONE query (e.g. 'airfryer'/'airfryert'), not two searches — merged.
_NEAR_VARIANT_RATIO = 0.9

# Search Intent blends the lexical buy-vs-curiosity tier with a REAL commercial
# signal — advertiser competition + CPC (advertisers only pay/compete on queries
# with genuine purchase value). Weights sum to 1.0.
_INTENT_LEXICAL_WEIGHT = 0.6
_INTENT_COMMERCIAL_WEIGHT = 0.4


# `keywords_for_keywords` aceita no máximo 20 seeds por task (limite do
# DataForSEO), e numa análise com marca esses 20 slots atendem DOIS assuntos: a
# marca (que é pontuada) e a categoria (que é contexto). A divisão é fixa em
# 14/6 porque previsível é auditável, e a marca leva a maior parte por ser o
# lado que entra no MOS. A sobra de um lado vai para o outro, então um produto
# sem categoria (ou sem marca) continua usando os 20.
#
# O construtor é ÚNICO de propósito: quem coleta (analysis_service) e quem lê
# (indicator_engine) precisam produzir exatamente a mesma lista, porque as
# related keywords são gravadas sob o rótulo `", ".join(seeds)`. Duas listas
# diferentes = rótulo diferente = intent órfão, que é o bug que a coleta
# forçada de `related_rows_exist` existe para não deixar acontecer.
_MAX_SEEDS = 20
_SEEDS_DA_MARCA = 14


def build_seed_terms(brand_terms: list[str], category_terms: list[str] | None = None) -> list[str]:
    """Os seeds de `keywords_for_keywords` para este país, marca primeiro."""
    categoria = [t for t in (category_terms or []) if t]
    marca = [t for t in brand_terms if t]
    if not categoria:
        return marca[:_MAX_SEEDS]
    if not marca:
        return categoria[:_MAX_SEEDS]

    cota_marca = min(_SEEDS_DA_MARCA, len(marca))
    cota_categoria = min(_MAX_SEEDS - cota_marca, len(categoria))
    # Sobra redistribuída: um lado curto não pode desperdiçar slot.
    cota_marca = min(len(marca), _MAX_SEEDS - cota_categoria)
    escolhidos = [*marca[:cota_marca], *categoria[:cota_categoria]]
    # Dedup preservando ordem: um termo pode estar nas duas listas.
    vistos: set[str] = set()
    saida: list[str] = []
    for termo in escolhidos:
        chave = termo.strip().lower()
        if chave and chave not in vistos:
            vistos.add(chave)
            saida.append(termo)
    return saida


def _norm_term(term: str) -> str:
    """Comparison key for a query: lowercase, strip accents, keep only
    alphanumerics. 'Air Fryer'/'air-fryer'/'airfryer' -> 'airfryer';
    'elétrica' -> 'eletrica'. Used to detect Google's LITERAL duplicates (same
    key AND same reported volume+series) — never to merge distinct-volume
    queries, which are real separate searches and are summed."""
    folded = unicodedata.normalize("NFKD", term).encode("ascii", "ignore").decode("ascii").lower()
    return "".join(ch for ch in folded if ch.isalnum())


def _de_outra_marca(keyword: str, outras_marcas: list[str] | None) -> bool:
    """A keyword relacionada carrega o nome de OUTRA marca cadastrada.

    POR QUE ISTO EXISTE. As keywords que o Google devolve para os termos
    semente de uma marca incluem as concorrentes — comparativos, listas,
    substituições. Medido no armazém real (Cardhu, GBR, 305 relacionadas): o
    filtro de relevância da expansão exige que a keyword contenha um termo
    aprovado, e como todos os oito termos aprovados carregavam "cardhu", as 21
    keywords sem a marca caíram sozinhas. Isso é sorte, não garantia: basta uma
    keyword conter ao mesmo tempo um termo aprovado e outra marca
    (`glenfiddich 12 vs cardhu 12`) para o volume de uma marca entrar na
    demanda da outra.

    O veto só age em produto COM marca. Numa entidade genérica — a categoria —
    consultas de marca são demanda legítima da categoria: quem procura "cardhu"
    está no mercado de whisky, e descartar isso subestimaria a categoria.

    Limite conhecido: compara formas normalizadas latinas, então a marca
    transliterada (`カーデュ` para Cardhu) não é reconhecida. Vale como guarda
    contra o caso comum, não como filtro completo.
    """
    if not outras_marcas:
        return False
    alvo = _norm_term(keyword)
    return any(marca and marca in alvo for marca in outras_marcas)


# Search Intent tiers/weights (Cap. 4 §4.2.6). Per-language markers live in
# intent_lexicon.py (offline-LLM-built, frozen). CURIOSITY is the discriminator:
# "what is / vs" = discovering (low purchase intent) vs "buy / price" = buying.
_TRANSACTIONAL_INTENT = 1.0
_EVALUATION_INTENT = 0.8
_DEFAULT_INTENT = 0.6  # unmatched: a product-variant/brand refinement
_USAGE_INTENT = 0.4
_CURIOSITY_INTENT = 0.2
_INTENT_FALLBACK_LANGUAGE = "en"


def _monthly_volumes(monthly_searches: Any) -> list[tuple[int, int, float]]:
    """Parse a keyword_search_volume.monthly_searches JSONB value into a list of
    (year, month, volume) tuples, coercing missing/None volumes to 0.0. Accepts
    either a already-decoded list or a JSON string."""
    if monthly_searches is None:
        return []
    data = json.loads(monthly_searches) if isinstance(monthly_searches, str) else monthly_searches
    out: list[tuple[int, int, float]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        year, month = item.get("year"), item.get("month")
        if year is None or month is None:
            continue
        vol = item.get("search_volume")
        out.append((int(year), int(month), 0.0 if vol is None else float(vol)))
    return out


async def _fetch_volume_rows(
    search_terms: list[str], location: str
) -> list[tuple[float, list[tuple[int, int, float]]]]:
    """Freshest row per approved term for this country → the distinct terms to
    SUM, after dropping ~zero-volume terms and collapsing Google's duplicate
    reports of the same query (see _dedup_volume_rows). Growth/Stability use this
    (approved terms only); the demand VOLUME additionally folds in expansion
    (see compute_trend_score). Returns (volume, monthly_series)."""
    return _dedup_volume_rows(await _fetch_approved_raw(search_terms, location))


async def _fetch_approved_raw(
    search_terms: list[str], location: str
) -> list[tuple[str, float, list[tuple[int, int, float]]]]:
    """Freshest (keyword, volume, monthly_series) per approved term — RAW, before
    the drop-zero/dedup pass (so callers can merge it with expansion rows first).

    The case-insensitive match lives in the collection service now. It matters:
    that source stores keywords lowercased ("ninja air fryer") while curated
    terms are mixed-case ("Ninja air fryer"), and an exact match silently
    dropped every brand and variant term from the volume sum."""
    if not search_terms:
        return []
    stored = await pytrends_stored_volume(location, search_terms)
    return [
        (
            item["keyword"],
            0.0 if item.get("search_volume") is None else float(item["search_volume"]),
            _monthly_volumes(item.get("monthly_searches")),
        )
        for item in stored["keywords"]
    ]


async def _fetch_related_rows(seed_label: str, location: str) -> list[dict[str, Any]]:
    """Freshest related keyword per (keyword) for this country's seed set, with
    volume + monthly series + commercial signals (cpc, competition_index). Shared
    by demand expansion (compute_trend_score) and intent (compute_search_intent_score)."""
    rows: list[dict[str, Any]] = []
    for item in await pytrends_stored_related(location, seed_label):
        if item.get("keyword") is None or item.get("search_volume") is None:
            continue
        rows.append(
            {
                "keyword": item["keyword"],
                "volume": float(item["search_volume"]),
                "monthly": _monthly_volumes(item.get("monthly_searches")),
                "cpc": None if item.get("cpc") is None else float(item["cpc"]),
                "competition_index": (
                    None if item.get("competition_index") is None else int(item["competition_index"])
                ),
            }
        )
    return rows


async def related_rows_exist(seed_terms: list[str], location: str) -> bool:
    """True when the collector already holds related keywords (with volume) for
    this EXACT seed set + location — the precondition compute_search_intent_score
    needs (it matches the stored rows on the joined seed label). Used to force a
    keywords_for_keywords re-collection when the seed set changed (e.g. a new
    identify-image overwrote localized_markets with different/longer terms): the
    old related rows no longer match the new label, which would otherwise leave
    search_intent_score UNAVAILABLE and abort the whole analysis."""
    if not seed_terms:
        return False
    seed_label = ", ".join(seed_terms)
    return any(
        (item.get("search_volume") or 0) > 0 for item in await pytrends_stored_related(location, seed_label)
    )


async def missing_volume_terms(terms: list[str], location: str) -> list[str]:
    """The terms with nothing stored for this location yet — exactly what a
    collection still needs. Enables term-incremental demand: only genuinely new
    terms are downloaded, already cached ones are skipped.

    The answer comes from the collection service rather than being worked out
    here. The match has to be case-insensitive, because that source lowercases
    what it stores, and a mixed-case curated term would otherwise look missing
    forever and be re-downloaded every run at real cost per call."""
    if not terms:
        return []
    stored = await pytrends_stored_volume(location, terms)
    return list(stored["missing"])


def _select_expansion(
    related: list[dict[str, Any]],
    approved_terms: list[str],
    tiers: dict[str, list[str]],
    outras_marcas: list[str] | None = None,
    excluded_terms: list[str] | None = None,
) -> list[tuple[str, float, list[tuple[int, int, float]]]]:
    """Conservatively pick related keywords to FOLD into the demand sum, WITHOUT
    any per-product exclusion list (that doesn't scale). A keyword qualifies when:
      - its volume clears the floor;
      - it does not carry ANOTHER registered brand's name (see _de_outra_marca);
      - it is clearly the product — its normalized form contains an approved
        product/brand term (drops unrelated products that bleed in);
      - its intent is purchase-oriented — NOT the lexicon's `usage`
        (recipe/how-to/clean = ownership) or `curiosity` (what-is/vs = browsing)
        tiers, which are not new demand. Kept: transactional/evaluation/default
        (buying, shopping/reviews, product variants).
    The lexicon is per-LANGUAGE and product-agnostic — the same one intent uses.
    (The LLM proposes the seeds; Google's related keywords confirm/expand what
    real buyers type.) Literal duplicates vs the approved terms are removed later
    by _dedup_volume_rows on the merged set. Top N by volume."""
    approved_norms = [n for n in (_norm_term(t) for t in approved_terms) if n]
    # The curation's own veto list, which the expansion never consulted. These
    # are terms that LOOK like the product and mean something else in this
    # market, so a keyword carrying one is not this product's demand however
    # many approved words it also carries: "picanha suína" is pork.
    excluded_norms = [n for n in (_norm_term(t) for t in (excluded_terms or [])) if n]
    qualifying: list[tuple[str, float, list[tuple[int, int, float]]]] = []
    for r in related:
        if r["volume"] < _VOLUME_FLOOR:
            continue
        if _de_outra_marca(r["keyword"], outras_marcas):
            continue
        kw_norm = _norm_term(r["keyword"])
        if any(x in kw_norm for x in excluded_norms):  # ruled out for this market
            continue
        if not any(a in kw_norm for a in approved_norms):  # relevance: contains a product term
            continue
        if _classify_query_intent(r["keyword"], tiers) < _DEFAULT_INTENT:  # drop usage/curiosity (not new demand)
            continue
        qualifying.append((r["keyword"], r["volume"], r["monthly"]))
    qualifying.sort(key=lambda x: x[1], reverse=True)
    return qualifying[:_EXPANSION_TOP_N]


def _tokens(term: str) -> frozenset[str]:
    """The words a query is made of, accent-folded. Order is not meaning: for
    Google Ads, "beef picanha steak" and "picanha beef steak" are the same
    search written twice."""
    folded = unicodedata.normalize("NFKD", term).encode("ascii", "ignore").decode("ascii").lower()
    return frozenset(w for w in re.split(r"[^a-z0-9]+", folded) if w)


def _dedup_volume_rows(
    raw: Iterable[tuple[str, float, list[tuple[int, int, float]]]],
) -> list[tuple[str, float, list[tuple[int, int, float]]]]:
    """Drop ~zero-volume rows and count each real query once.

    Google Ads reports a close-variant GROUP — one search, written many ways —
    by repeating the identical volume under every spelling it recognises. For
    picanha in the US it returned:

        picanha steak / picaña steak / picanha beef steak / beef picanha steak
            ... all at exactly 90,500
        coulotte beef / coulotte steak / sirloin coulotte / ... (nine of them)
            ... all at exactly 18,100

    Summed, those nine coulotte spellings become 162,900 searches that do not
    exist. The rule that stood here only merged strings that LOOKED alike at
    the same volume, so it caught "airfryer"/"airfryert" and missed every case
    above — and the damage is not a rounding error. It inflated one country's
    demand by whatever number of spellings its expansion happened to return,
    which is different per country and per run, and that figure is the ruler
    every other country's demand is measured against. Picanha read as a weak
    product in Brazil because of it.

    So: an identical reported volume plus at least one shared word is one
    query, counted once. An identical volume alone is not enough — Google
    rounds to coarse buckets and two unrelated searches can land in the same
    one.

    The cost is a real undercount when two genuinely different searches for the
    same product share a bucket ("picanha kg" and "picanha wagyu", both 3,600,
    become 3,600). That is bounded and small. The alternative was unbounded and
    varied by country, which is worse in a number whose whole purpose is
    comparing countries.

    Input rows are (keyword, volume, monthly_series); the keyword survives,
    because the per-term breakdown in the evidence has to read as real words.
    """
    groups: list[dict[str, Any]] = []
    for keyword, volume, monthly in raw:
        if volume < _VOLUME_FLOOR:
            continue
        norm, tokens = _norm_term(keyword), _tokens(keyword)

        for group in groups:
            if group["volume"] != volume:
                continue
            # Same query written differently: an exact word-set match is the
            # clearest case, a shared word plus the identical volume the
            # general one, and the old look-alike rule still catches typo
            # variants that share no whole word ("airfryer"/"airfryert").
            same_words = tokens and tokens == group["tokens"]
            shares_a_word = bool(tokens & group["tokens"])
            looks_alike = SequenceMatcher(None, norm, group["norm"]).ratio() >= _NEAR_VARIANT_RATIO
            if same_words or shares_a_word or looks_alike:
                # The shortest spelling is the one a person reading the
                # breakdown recognises: "picanha steak", not "beef picanha
                # steak roast".
                if len(keyword) < len(group["keyword"]):
                    group["keyword"], group["norm"], group["tokens"] = keyword, norm, tokens
                # And the longest monthly series, since the variants report
                # slightly different ones for the same search.
                if len(monthly) > len(group["monthly"]):
                    group["monthly"] = monthly
                break
        else:
            groups.append(
                {"keyword": keyword, "norm": norm, "tokens": tokens, "volume": volume, "monthly": monthly}
            )

    return [(g["keyword"], g["volume"], g["monthly"]) for g in groups]


def _summed_monthly_series(rows: list[tuple[str, float, list[tuple[int, int, float]]]]) -> list[float]:
    """Element-wise sum of the deduped terms' monthly_searches, aligned by
    (year, month) and returned oldest-first — the product's total monthly
    demand series in this country."""
    totals: dict[tuple[int, int], float] = {}
    for _keyword, _volume, monthly in rows:
        for year, month, vol in monthly:
            totals[(year, month)] = totals.get((year, month), 0.0) + vol
    return [totals[k] for k in sorted(totals)]


async def _demand_rows(
    search_terms: list[str],
    location: str,
    *,
    seed_terms: list[str] | None = None,
    language: str | None = None,
    outras_marcas: list[str] | None = None,
    excluded_terms: list[str] | None = None,
    expandir: bool = True,
) -> tuple[list[tuple[str, float, list[tuple[int, int, float]]]], int]:
    """As linhas de demanda do produto neste país: termos aprovados MAIS a
    expansão conservadora de keywords relacionadas, deduplicadas.

    POR QUE OS TRÊS INDICADORES LEEM O MESMO CONJUNTO. Até 2026-08-25 só o
    Demand Volume dobrava a expansão; Growth e Stability liam apenas os termos
    aprovados. Como `_dedup_volume_rows` descarta termo de volume ~zero, um
    produto cujos termos CURADOS não têm volume no país ficava com série mensal
    vazia — e os dois indicadores voltavam UNAVAILABLE, derrubando a análise
    inteira pelo portão de completude, enquanto o Demand Volume do MESMO país
    tinha sido calculado sem problema com as keywords relacionadas.

    Aconteceu em produção (análise e152761b, `3D printer filament`): BRA, PRT e
    ESP perderam `growth_score` e `demand_stability_score`, e a análise falhou
    com o volume de demanda dos três já calculado.

    Não é imputação: é usar o dado que o sistema já coletou e no qual já confia
    o suficiente para definir a demanda do produto. Excluí-lo de dois dos
    quatro sinais da mesma dimensão era inconsistência, não salvaguarda.

    Medido no armazém do PyTrends: a série mensal é binária — 12 meses (1.470
    linhas) ou nenhum (190), e NENHUMA linha com série curta tem volume >= 10.
    Ou seja, série vazia sempre significa "estes termos não têm volume aqui",
    nunca "o dado veio truncado".
    """
    approved = await _fetch_approved_raw(search_terms, location)
    if not search_terms or not expandir:
        return _dedup_volume_rows(approved), 0
    seed_label = ", ".join(seed_terms or search_terms[:20])
    related = await _fetch_related_rows(seed_label, location)
    tiers = INTENT_LEXICON.get(language or "", INTENT_LEXICON[_INTENT_FALLBACK_LANGUAGE])
    expansao = _select_expansion(related, search_terms, tiers, outras_marcas, excluded_terms)
    return _dedup_volume_rows(approved + expansao), len(expansao)


async def compute_trend_score(
    search_terms: list[str],
    location: str,
    period_key: str,
    *,
    seed_terms: list[str] | None = None,
    language: str | None = None,
    outras_marcas: list[str] | None = None,
    excluded_terms: list[str] | None = None,
    expandir: bool = True,
) -> RawIndicatorResult:
    """Cap. 4 §4.2.1 — DEMAND VOLUME (market potential): SUM of the absolute
    monthly search volume of the country's approved terms PLUS a conservative
    expansion of high-volume related keywords that are clearly the product
    (the LLM proposes seeds; Google's related keywords confirm/expand). ~Zero-
    volume terms are dropped and only Google's literal duplicates are collapsed
    (see _dedup_volume_rows) — distinct real queries are summed. The raw value
    IS that absolute count, normalized against the product's reference-market
    volume downstream. Same pipeline for every country AND the reference-market
    candidates, so the ruler stays comparable. Penetration vs population is
    added by the indicator engine as decision context."""
    rows, expansion_count = await _demand_rows(
        search_terms,
        location,
        seed_terms=seed_terms,
        language=language,
        outras_marcas=outras_marcas,
        excluded_terms=excluded_terms,
        expandir=expandir,
    )

    if not rows:
        return RawIndicatorResult(
            raw_value=None,
            direction=IndicatorDirection.DIRECT,
            calculation_window=period_key,
            availability_status=AvailabilityStatus.UNAVAILABLE,
            notes=f"keyword_search_volume: no rows for location='{location}' and the approved terms (Cap. 4 §4.2.1)",
        )

    total_volume = sum(volume for _keyword, volume, _monthly in rows)
    expansion_note = f" (+{expansion_count} folded from related keywords)" if expansion_count else ""
    # Per-term breakdown (the searched words + their monthly volumes) so the final
    # narrative can list, per country, exactly which words were searched, how much
    # each, and the total. Ordered highest-volume first, all summed terms shown.
    per_term = sorted(((kw, vol) for kw, vol, _monthly in rows), key=lambda kv: kv[1], reverse=True)
    breakdown = "; ".join(f"{kw} {vol:,.0f}" for kw, vol in per_term)
    notes = (
        f"{total_volume:,.0f} total monthly searches over {len(rows)} distinct term(s){expansion_note}. "
        f"Per-term monthly search volume — {breakdown} (Cap. 4 §4.2.1)"
    )
    return RawIndicatorResult(
        raw_value=total_volume,
        direction=IndicatorDirection.DIRECT,
        calculation_window=period_key,
        availability_status=AvailabilityStatus.AVAILABLE,
        notes=notes,
    )


async def compute_growth_score(
    search_terms: list[str],
    location: str,
    period_key: str,
    *,
    seed_terms: list[str] | None = None,
    language: str | None = None,
    outras_marcas: list[str] | None = None,
) -> RawIndicatorResult:
    """Cap. 4 §4.2.2 — GrowthRaw = (final_avg - initial_avg)/max(initial_avg,1)*100
    over the SUMMED monthly_searches series (trailing 12 months), k = n//3."""
    rows, _ = await _demand_rows(
        search_terms, location, seed_terms=seed_terms, language=language, outras_marcas=outras_marcas
    )
    series = _summed_monthly_series(rows)

    n = len(series)
    if n < 6:
        return RawIndicatorResult(
            raw_value=None,
            direction=IndicatorDirection.DIRECT,
            calculation_window=period_key,
            availability_status=AvailabilityStatus.UNAVAILABLE,
            notes=f"monthly_searches: only {n} month(s), need >= 6 (Cap. 4 §4.2.2)",
        )

    k = max(1, n // 3)
    initial_avg = sum(series[:k]) / k
    final_avg = sum(series[n - k :]) / k

    # Low-base noise guard: a near-zero initial window makes the ratio explode.
    if initial_avg < _MIN_GROWTH_BASELINE:
        return RawIndicatorResult(
            raw_value=0.0,
            direction=IndicatorDirection.DIRECT,
            calculation_window=period_key,
            availability_status=AvailabilityStatus.AVAILABLE,
            notes=f"initial-window mean {initial_avg:.1f} below {_MIN_GROWTH_BASELINE} searches — "
            "flat 0% growth (near-zero base, no measurable trend)",
        )

    growth_raw = ((final_avg - initial_avg) / initial_avg) * 100
    return RawIndicatorResult(
        raw_value=growth_raw,
        direction=IndicatorDirection.DIRECT,
        calculation_window=period_key,
        availability_status=AvailabilityStatus.AVAILABLE,
        notes=f"n={n}, k={k}, initial={initial_avg:.0f}, final={final_avg:.0f}",
    )


async def compute_demand_stability_score(
    search_terms: list[str],
    location: str,
    period_key: str,
    *,
    seed_terms: list[str] | None = None,
    language: str | None = None,
    outras_marcas: list[str] | None = None,
) -> RawIndicatorResult:
    """Cap. 4 §4.2.3 — StabilityRaw = CV = stddev/mean (population stddev) of the
    SUMMED monthly_searches series."""
    rows, _ = await _demand_rows(
        search_terms, location, seed_terms=seed_terms, language=language, outras_marcas=outras_marcas
    )
    series = _summed_monthly_series(rows)

    if len(series) < 2:
        return RawIndicatorResult(
            raw_value=None,
            direction=IndicatorDirection.INVERSE,
            calculation_window=period_key,
            availability_status=AvailabilityStatus.UNAVAILABLE,
            notes=f"monthly_searches: only {len(series)} month(s) (Cap. 4 §4.2.3)",
        )

    mean = sum(series) / len(series)
    # Low-volume guard: a near-zero, spiky series makes CV explode/undefined.
    if mean < _MIN_STABILITY_BASELINE:
        return RawIndicatorResult(
            raw_value=_NEUTRAL_STABILITY_CV,
            direction=IndicatorDirection.INVERSE,
            calculation_window=period_key,
            availability_status=AvailabilityStatus.AVAILABLE,
            notes=f"series mean {mean:.1f} below {_MIN_STABILITY_BASELINE} searches — "
            "low-volume, stability not reliably measurable, neutral score",
        )

    variance = sum((v - mean) ** 2 for v in series) / len(series)
    stddev = variance**0.5
    cv = stddev / mean
    return RawIndicatorResult(
        raw_value=cv,
        direction=IndicatorDirection.INVERSE,
        calculation_window=period_key,
        availability_status=AvailabilityStatus.AVAILABLE,
        notes=f"n={len(series)}, mean={mean:.0f}, stddev={stddev:.0f}",
    )


def _classify_query_intent(query: str, tiers: dict[str, list[str]]) -> float:
    """Classify one related keyword against a language's intent lexicon
    (`tiers` = INTENT_LEXICON[language]). Transactional first (strongest), then
    curiosity (weakest — separates buying from browsing), then evaluation, then
    usage; unmatched -> a mild product-variant/brand refinement."""
    lowered = f" {query.lower()} "  # pad so single-token markers like "vs" match at edges
    if any(term in lowered for term in tiers.get("transactional", ())):
        return _TRANSACTIONAL_INTENT
    if any(term in lowered for term in tiers.get("curiosity", ())):
        return _CURIOSITY_INTENT
    if any(term in lowered for term in tiers.get("evaluation", ())):
        return _EVALUATION_INTENT
    if any(term in lowered for term in tiers.get("usage", ())):
        return _USAGE_INTENT
    return _DEFAULT_INTENT


async def compute_search_intent_score(
    seed_terms: list[str],
    location: str,
    period_key: str,
    language: str | None = None,
    outras_marcas: list[str] | None = None,
    relevant_terms: list[str] | None = None,
) -> RawIndicatorResult:
    """Cap. 4 §4.2.6 — IntentRaw = volume-weighted average of a lexical buy-vs-
    curiosity classification over the country's related keywords (Google Ads
    `keyword_related` for the product's seed terms). Weighting by REAL volume
    (vs the old relative Trends values) makes a market whose volume concentrates
    on 'buy/price/where to buy' score high, and one on 'what is/recipe' score
    low. Queries are classified with the country's search_language lexicon
    (frozen intent_lexicon.py); unknown language falls back to English."""
    tiers = INTENT_LEXICON.get(language or "", INTENT_LEXICON[_INTENT_FALLBACK_LANGUAGE])
    if not seed_terms:
        return RawIndicatorResult(
            raw_value=None,
            direction=IndicatorDirection.DIRECT,
            calculation_window=period_key,
            availability_status=AvailabilityStatus.UNAVAILABLE,
            notes="no seed terms for related-keyword intent (Cap. 4 §4.2.6)",
        )
    # The collector stores one keyword_related row set per keywords-for-keywords
    # call, tagged with seed_keyword = ", ".join(seed_keywords) — the whole seed
    # SET, since the related keywords are for the set, not per individual seed.
    # Match that exact label (analysis_service sends the same search_terms[:20]).
    seed_label = ", ".join(seed_terms)
    related = await _fetch_related_rows(seed_label, location)
    # Mesmo veto da expansão: a intenção de compra desta marca não pode ser
    # composta pelas keywords de outra. Sem isto, um comparativo de alto volume
    # da concorrente pesaria na intenção deste produto.
    rows = [r for r in related if r["volume"] > 0 and not _de_outra_marca(r["keyword"], outras_marcas)]
    # `relevant_terms` só vem preenchido quando o conjunto de seeds é
    # COMPARTILHADO entre dois assuntos (marca + categoria, na mesma request do
    # DataForSEO para não pagar duas vezes). Aí as related voltam todas sob o
    # mesmo rótulo e a intenção da marca seria composta também pelas keywords
    # genéricas. Com um assunto só, o portão fica desligado de propósito: as
    # related são todas dele, e exigir que contenham o nome do produto
    # descartaria keyword local legítima que não o repete.
    if relevant_terms:
        relevantes = [n for n in (_norm_term(t) for t in relevant_terms) if n]
        rows = [r for r in rows if any(n in _norm_term(r["keyword"]) for n in relevantes)]

    if not rows:
        return RawIndicatorResult(
            raw_value=None,
            direction=IndicatorDirection.DIRECT,
            calculation_window=period_key,
            availability_status=AvailabilityStatus.UNAVAILABLE,
            notes="keyword_related: no related keywords with volume (Cap. 4 §4.2.6)",
        )

    total_weight = sum(r["volume"] for r in rows)
    # Commercial signal, normalized within this country's related set:
    # advertiser competition (0-100) and CPC (relative to the set's max) — both
    # proxy real purchase value (advertisers only pay/compete on money queries).
    max_cpc = max((r["cpc"] or 0.0) for r in rows)

    def _commercial(r: dict[str, Any]) -> float | None:
        signals: list[float] = []
        if r["competition_index"] is not None:
            signals.append(r["competition_index"] / 100.0)
        if r["cpc"] is not None and max_cpc > 0:
            signals.append(min(1.0, r["cpc"] / max_cpc))
        return sum(signals) / len(signals) if signals else None

    def _intent(r: dict[str, Any]) -> float:
        lexical = _classify_query_intent(r["keyword"], tiers)
        commercial = _commercial(r)
        if commercial is None:  # no CPC/competition data — fall back to pure lexical
            return lexical
        return _INTENT_LEXICAL_WEIGHT * lexical + _INTENT_COMMERCIAL_WEIGHT * commercial

    weighted_sum = sum(r["volume"] * _intent(r) for r in rows)
    intent_raw = (weighted_sum / total_weight) * 100
    return RawIndicatorResult(
        raw_value=intent_raw,
        direction=IndicatorDirection.DIRECT,
        calculation_window=period_key,
        availability_status=AvailabilityStatus.AVAILABLE,
        notes=f"{len(rows)} related keywords, volume-weighted lexical intent + commercial (CPC/competition)",
    )
