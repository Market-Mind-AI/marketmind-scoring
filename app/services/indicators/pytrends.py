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
  - LEVEL (trend_score): SUM of the absolute monthly search volume of the
    country's `search_terms` (deduped), divided by the country's internet users
    -> demand INTENSITY per online buyer (raw is searches per 1,000 internet
    users; market SIZE is a separate dimension, so we don't double-count it).
  - GROWTH: trend of the SUMMED monthly_searches series (trailing 12 months).
  - DEMAND STABILITY: CV of that same summed monthly series.
  - SEARCH INTENT: related keywords (`keyword_related`) classified buy-vs-
    curiosity, weighted by their real volume.
"""

import json
import math
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
    """Freshest row per approved term for this country, deduped by identical
    (search_volume, monthly_searches): Google Ads canonicalizes near-variants
    (e.g. 'air fryer'/'airfryer') and reports the SAME numbers for them, so
    summing raw would double-count the same reported volume. Distinct real
    terms (different numbers) are kept and summed — their searches genuinely
    happened. Returns (search_volume, monthly_series) per distinct entity."""
    if not search_terms:
        return []
    result = await connection.execute(
        text(
            "SELECT DISTINCT ON (keyword) keyword, search_volume, monthly_searches "
            "FROM keyword_search_volume WHERE location = :location AND keyword = ANY(:terms) "
            "ORDER BY keyword, created_at DESC"
        ),
        {"location": location, "terms": search_terms},
    )
    seen: set[tuple[float, tuple[tuple[int, int, float], ...]]] = set()
    rows: list[tuple[float, list[tuple[int, int, float]]]] = []
    for row in result.all():
        volume = 0.0 if row[1] is None else float(row[1])
        monthly = _monthly_volumes(row[2])
        key = (volume, tuple(monthly))
        if key in seen:
            continue
        seen.add(key)
        rows.append((volume, monthly))
    return rows


def _summed_monthly_series(rows: list[tuple[float, list[tuple[int, int, float]]]]) -> list[float]:
    """Element-wise sum of the deduped terms' monthly_searches, aligned by
    (year, month) and returned oldest-first — the product's total monthly
    demand series in this country."""
    totals: dict[tuple[int, int], float] = {}
    for _volume, monthly in rows:
        for year, month, vol in monthly:
            totals[(year, month)] = totals.get((year, month), 0.0) + vol
    return [totals[k] for k in sorted(totals)]


async def compute_trend_score(search_terms: list[str], location: str, period_key: str) -> RawIndicatorResult:
    """Cap. 4 §4.2.1 — cross-country demand LEVEL = SUM of the absolute monthly
    search volume of the country's approved terms (deduped): the total real
    online search demand for the product in that country (user decision
    2026-07-15). The raw value is ln(total) so the 0-100 normalization is
    LOGARITHMIC (same transform as population_scale): a big market scores clearly
    higher (more real sales potential) WITHOUT crushing a small-but-strong market
    to zero — the concrete total is kept in the notes as decision context."""
    engine = get_external_engine(DataSource.PYTRENDS)
    async with engine.connect() as connection:
        rows = await _fetch_volume_rows(connection, search_terms, location)

    if not rows:
        return RawIndicatorResult(
            raw_value=None,
            direction=IndicatorDirection.DIRECT,
            calculation_window=period_key,
            availability_status=AvailabilityStatus.UNAVAILABLE,
            notes=f"keyword_search_volume: no rows for location='{location}' and the approved terms (Cap. 4 §4.2.1)",
        )

    total_volume = sum(volume for volume, _monthly in rows)
    return RawIndicatorResult(
        raw_value=math.log(max(total_volume, 1.0)),  # ln bounds; max(.,1) so 0 demand -> ln(1)=0 (floor), not undefined
        direction=IndicatorDirection.DIRECT,
        calculation_window=period_key,
        availability_status=AvailabilityStatus.AVAILABLE,
        notes=f"SUM {total_volume:.0f} monthly searches over {len(rows)} distinct term(s), ln-norm (Cap. 4 §4.2.1)",
    )


async def compute_growth_score(search_terms: list[str], location: str, period_key: str) -> RawIndicatorResult:
    """Cap. 4 §4.2.2 — GrowthRaw = (final_avg - initial_avg)/max(initial_avg,1)*100
    over the SUMMED monthly_searches series (trailing 12 months), k = n//3."""
    engine = get_external_engine(DataSource.PYTRENDS)
    async with engine.connect() as connection:
        rows = await _fetch_volume_rows(connection, search_terms, location)
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
    search_terms: list[str], location: str, period_key: str
) -> RawIndicatorResult:
    """Cap. 4 §4.2.3 — StabilityRaw = CV = stddev/mean (population stddev) of the
    SUMMED monthly_searches series."""
    engine = get_external_engine(DataSource.PYTRENDS)
    async with engine.connect() as connection:
        rows = await _fetch_volume_rows(connection, search_terms, location)
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
        result = await connection.execute(
            text(
                "SELECT DISTINCT ON (keyword) keyword, search_volume FROM keyword_related "
                "WHERE location = :location AND seed_keyword = :seed_label "
                "ORDER BY keyword, created_at DESC"
            ),
            {"location": location, "seed_label": seed_label},
        )
        rows = [(row[0], float(row[1])) for row in result.all() if row[0] is not None and row[1] is not None]

    if not rows:
        return RawIndicatorResult(
            raw_value=None,
            direction=IndicatorDirection.DIRECT,
            calculation_window=period_key,
            availability_status=AvailabilityStatus.UNAVAILABLE,
            notes="keyword_related: no related keywords with volume (Cap. 4 §4.2.6)",
        )

    total_weight = sum(w for _, w in rows)
    if total_weight == 0:
        return RawIndicatorResult(
            raw_value=None,
            direction=IndicatorDirection.DIRECT,
            calculation_window=period_key,
            availability_status=AvailabilityStatus.UNAVAILABLE,
            notes="keyword_related: all volumes are 0 (Cap. 4 §4.2.6)",
        )

    weighted_sum = sum(w * _classify_query_intent(q, tiers) for q, w in rows)
    intent_raw = (weighted_sum / total_weight) * 100
    return RawIndicatorResult(
        raw_value=intent_raw,
        direction=IndicatorDirection.DIRECT,
        calculation_window=period_key,
        availability_status=AvailabilityStatus.AVAILABLE,
        notes=f"{len(rows)} related keywords classified, volume-weighted",
    )
