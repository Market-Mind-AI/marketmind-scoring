"""Cap. 4 §4.2 — raw value computation for the Demand indicators, now sourced
from Google Ads keyword data (ABSOLUTE monthly search volume via DataForSEO),
NOT Google Trends. Reads read-only from the collector service's own database
(app/db/external.py, DataSource.PYTRENDS) — the `keyword_search_volume` and
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
import unicodedata
from collections.abc import Iterable
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.external import get_external_engine
from app.domain.enums import AvailabilityStatus, DataSource, IndicatorDirection
from app.services.indicators.intent_lexicon import INTENT_LEXICON
from app.services.indicators.results import RawIndicatorResult

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


def _norm_term(term: str) -> str:
    """Comparison key for a query: lowercase, strip accents, keep only
    alphanumerics. 'Air Fryer'/'air-fryer'/'airfryer' -> 'airfryer';
    'elétrica' -> 'eletrica'. Used to detect Google's LITERAL duplicates (same
    key AND same reported volume+series) — never to merge distinct-volume
    queries, which are real separate searches and are summed."""
    folded = unicodedata.normalize("NFKD", term).encode("ascii", "ignore").decode("ascii").lower()
    return "".join(ch for ch in folded if ch.isalnum())


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
    connection: AsyncConnection, search_terms: list[str], location: str
) -> list[tuple[float, list[tuple[int, int, float]]]]:
    """Freshest row per approved term for this country → the distinct terms to
    SUM, after dropping ~zero-volume terms and collapsing Google's duplicate
    reports of the same query (see _dedup_volume_rows). Growth/Stability use this
    (approved terms only); the demand VOLUME additionally folds in expansion
    (see compute_trend_score). Returns (volume, monthly_series)."""
    return _dedup_volume_rows(await _fetch_approved_raw(connection, search_terms, location))


async def _fetch_approved_raw(
    connection: AsyncConnection, search_terms: list[str], location: str
) -> list[tuple[str, float, list[tuple[int, int, float]]]]:
    """Freshest (keyword, volume, monthly_series) per approved term — RAW, before
    the drop-zero/dedup pass (so callers can merge it with expansion rows first)."""
    if not search_terms:
        return []
    # Case-INSENSITIVE match: DataForSEO stores keywords lowercased (e.g.
    # "ninja air fryer"), while curated terms are mixed-case ("Ninja air fryer").
    # An exact match silently dropped every brand/variant term from the volume
    # sum (undercount) — lower() on both sides makes the curated terms actually
    # count. DISTINCT ON the normalized keyword also collapses case duplicates.
    lowered = [t.strip().lower() for t in search_terms]
    result = await connection.execute(
        text(
            "SELECT DISTINCT ON (lower(keyword)) keyword, search_volume, monthly_searches "
            "FROM keyword_search_volume WHERE location = :location AND lower(keyword) = ANY(:lowered) "
            "ORDER BY lower(keyword), created_at DESC"
        ),
        {"location": location, "lowered": lowered},
    )
    return [(row[0], 0.0 if row[1] is None else float(row[1]), _monthly_volumes(row[2])) for row in result.all()]


async def _fetch_related_rows(connection: AsyncConnection, seed_label: str, location: str) -> list[dict[str, Any]]:
    """Freshest related keyword per (keyword) for this country's seed set, with
    volume + monthly series + commercial signals (cpc, competition_index). Shared
    by demand expansion (compute_trend_score) and intent (compute_search_intent_score)."""
    result = await connection.execute(
        text(
            "SELECT DISTINCT ON (keyword) keyword, search_volume, monthly_searches, cpc, competition_index "
            "FROM keyword_related WHERE location = :location AND seed_keyword = :seed_label "
            "ORDER BY keyword, created_at DESC"
        ),
        {"location": location, "seed_label": seed_label},
    )
    rows: list[dict[str, Any]] = []
    for row in result.all():
        if row[0] is None or row[1] is None:
            continue
        rows.append(
            {
                "keyword": row[0],
                "volume": float(row[1]),
                "monthly": _monthly_volumes(row[2]),
                "cpc": None if row[3] is None else float(row[3]),
                "competition_index": None if row[4] is None else int(row[4]),
            }
        )
    return rows


async def related_rows_exist(seed_terms: list[str], location: str) -> bool:
    """True when the collector already holds related keywords (with volume) for
    this EXACT seed set + location — the precondition compute_search_intent_score
    needs (it matches keyword_related on the joined seed label). Used to force a
    keywords_for_keywords re-collection when the seed set changed (e.g. a new
    identify-image overwrote localized_markets with different/longer terms): the
    old related rows no longer match the new label, which would otherwise leave
    search_intent_score UNAVAILABLE and abort the whole analysis."""
    if not seed_terms:
        return False
    seed_label = ", ".join(seed_terms)
    engine = get_external_engine(DataSource.PYTRENDS)
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT 1 FROM keyword_related "
                "WHERE location = :location AND seed_keyword = :seed_label AND search_volume > 0 LIMIT 1"
            ),
            {"location": location, "seed_label": seed_label},
        )
        return result.first() is not None


async def missing_volume_terms(terms: list[str], location: str) -> list[str]:
    """The subset of `terms` with NO keyword_search_volume row yet for this
    location — i.e. exactly what a collection still needs. Enables
    term-incremental demand: only genuinely new terms are downloaded, already
    cached ones are skipped ("só baixa se um termo novo aparece"). Matches on the
    exact keyword string, the same key compute_trend_score reads by."""
    if not terms:
        return []
    # Case-insensitive, matching how the trend lookup reads (DataForSEO stores
    # keywords lowercased) — otherwise mixed-case curated terms would look
    # "missing" forever and re-download every run.
    lowered = [t.strip().lower() for t in terms]
    engine = get_external_engine(DataSource.PYTRENDS)
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT DISTINCT lower(keyword) FROM keyword_search_volume "
                "WHERE location = :location AND lower(keyword) = ANY(:lowered)"
            ),
            {"location": location, "lowered": lowered},
        )
        have = {row[0] for row in result.all()}
    return [t for t, lt in zip(terms, lowered, strict=True) if lt not in have]


def _select_expansion(
    related: list[dict[str, Any]], approved_terms: list[str], tiers: dict[str, list[str]]
) -> list[tuple[str, float, list[tuple[int, int, float]]]]:
    """Conservatively pick related keywords to FOLD into the demand sum, WITHOUT
    any per-product exclusion list (that doesn't scale). A keyword qualifies when:
      - its volume clears the floor;
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
    qualifying: list[tuple[str, float, list[tuple[int, int, float]]]] = []
    for r in related:
        if r["volume"] < _VOLUME_FLOOR:
            continue
        kw_norm = _norm_term(r["keyword"])
        if not any(a in kw_norm for a in approved_norms):  # relevance: contains a product term
            continue
        if _classify_query_intent(r["keyword"], tiers) < _DEFAULT_INTENT:  # drop usage/curiosity (not new demand)
            continue
        qualifying.append((r["keyword"], r["volume"], r["monthly"]))
    qualifying.sort(key=lambda x: x[1], reverse=True)
    return qualifying[:_EXPANSION_TOP_N]


def _dedup_volume_rows(
    raw: Iterable[tuple[str, float, list[tuple[int, int, float]]]],
) -> list[tuple[str, float, list[tuple[int, int, float]]]]:
    """Drop ~zero-volume rows and collapse Google's duplicate reports of the SAME
    query so its volume is counted once. Two passes:

    1. (normalized string, volume) — same normalized term at the same reported
       volume is one query, whether it differs only in case/accent/spacing OR
       arrives from both the approved and related tables (series is deliberately
       NOT in the key, since the two tables report slightly different series for
       the same term). Distinct real queries with DIFFERENT volumes ('air fryer'
       550k vs 'airfryer' 74k) keep separate keys and are summed.
    2. Within an IDENTICAL volume, collapse near-identical strings — Google's
       close-variant grouping reports the same number under typo variants
       ('airfryer'/'airfryert'). Never merges different volumes or genuinely
       different words ('best air fryer'), so real distinct demand is preserved.

    Input rows are (keyword, volume, monthly_series); output keeps the keyword:
    (keyword, volume, series) — the keyword is needed for the per-term demand
    breakdown surfaced in the evidence packet / narrative."""
    # Keep the ORIGINAL keyword for display (the per-term breakdown must read as
    # real words, e.g. "fritadeira eletrica", not the space-stripped norm), while
    # using the normalized form only for dedup keys / near-variant matching.
    seen: set[tuple[str, float]] = set()
    deduped: list[tuple[str, str, float, list[tuple[int, int, float]]]] = []
    for keyword, volume, monthly in raw:
        if volume < _VOLUME_FLOOR:
            continue
        norm = _norm_term(keyword)
        key = (norm, volume)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((keyword, norm, volume, monthly))

    kept: list[tuple[str, str, float, list[tuple[int, int, float]]]] = []
    for keyword, norm, volume, monthly in deduped:
        if any(
            volume == kv and SequenceMatcher(None, norm, kn).ratio() >= _NEAR_VARIANT_RATIO for _k, kn, kv, _m in kept
        ):
            continue
        kept.append((keyword, norm, volume, monthly))
    return [(keyword, volume, monthly) for keyword, _norm, volume, monthly in kept]


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
    connection: AsyncConnection,
    search_terms: list[str],
    location: str,
    *,
    seed_terms: list[str] | None = None,
    language: str | None = None,
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
    approved = await _fetch_approved_raw(connection, search_terms, location)
    if not search_terms:
        return _dedup_volume_rows(approved), 0
    seed_label = ", ".join(seed_terms or search_terms[:20])
    related = await _fetch_related_rows(connection, seed_label, location)
    tiers = INTENT_LEXICON.get(language or "", INTENT_LEXICON[_INTENT_FALLBACK_LANGUAGE])
    expansao = _select_expansion(related, search_terms, tiers)
    return _dedup_volume_rows(approved + expansao), len(expansao)


async def compute_trend_score(
    search_terms: list[str],
    location: str,
    period_key: str,
    *,
    seed_terms: list[str] | None = None,
    language: str | None = None,
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
    engine = get_external_engine(DataSource.PYTRENDS)
    async with engine.connect() as connection:
        rows, expansion_count = await _demand_rows(
            connection, search_terms, location, seed_terms=seed_terms, language=language
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
) -> RawIndicatorResult:
    """Cap. 4 §4.2.2 — GrowthRaw = (final_avg - initial_avg)/max(initial_avg,1)*100
    over the SUMMED monthly_searches series (trailing 12 months), k = n//3."""
    engine = get_external_engine(DataSource.PYTRENDS)
    async with engine.connect() as connection:
        rows, _ = await _demand_rows(connection, search_terms, location, seed_terms=seed_terms, language=language)
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
) -> RawIndicatorResult:
    """Cap. 4 §4.2.3 — StabilityRaw = CV = stddev/mean (population stddev) of the
    SUMMED monthly_searches series."""
    engine = get_external_engine(DataSource.PYTRENDS)
    async with engine.connect() as connection:
        rows, _ = await _demand_rows(connection, search_terms, location, seed_terms=seed_terms, language=language)
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
    seed_terms: list[str], location: str, period_key: str, language: str | None = None
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
    engine = get_external_engine(DataSource.PYTRENDS)
    async with engine.connect() as connection:
        related = await _fetch_related_rows(connection, seed_label, location)
    rows = [r for r in related if r["volume"] > 0]

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
