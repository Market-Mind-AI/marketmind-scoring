"""How long a product's search vocabulary stays fixed, and how a new one is
merged into the stored one.

One rule, read by every place that stores search terms or reference
candidates: the product's own (`product_catalog.localized_markets` and
`reference_candidates`) and a brand's (`brand_markets` and
`brand_reference_candidates`). See `search_terms_ttl_days` for why the window
exists.

The two sides used to follow different rules. The product merged inside the
window only the countries it lacked and accumulated outside it; the brand
REPLACED a country's terms outside the window, and the brand's reference
candidates had no window at all — a new identification overwrote them, and a
new term list is a new seed for the related keywords, bought again on Google
Ads in every country.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.config import settings
from app.utils.text import as_term_list
from app.utils.time import utc_now


def vocabulary_is_fixed(fixed_at: datetime | None) -> bool:
    """True while a vocabulary written at `fixed_at` is still inside the
    semester window (`search_terms_ttl_days`). None means "never fixed": the
    next identification may write it."""
    if fixed_at is None:
        return False
    if fixed_at.tzinfo is None:
        fixed_at = fixed_at.replace(tzinfo=UTC)
    return utc_now() - fixed_at < timedelta(days=settings.search_terms_ttl_days)


def union_terms(*lists: list[str] | None) -> list[str]:
    """Case-insensitive union preserving first-seen order (existing terms first).
    Curated keywords are ACCUMULATED, never replaced — a re-identify only appends
    genuinely new terms, keeping the seed order (and thus the demand cache) stable."""
    seen: set[str] = set()
    out: list[str] = []
    for lst in lists:
        for term in lst or []:
            if not term:
                continue
            key = str(term).strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(term)
    return out


def merge_markets(
    existing: dict[str, dict[str, Any]] | None, incoming: dict[str, dict[str, Any]] | None
) -> dict[str, dict[str, Any]]:
    """Merge per-country search vocabularies: union search_terms/excluded_terms
    (existing first + new appended), so nothing curated is ever lost and only new
    terms are added to the base. The language the terms were written in is kept
    — the stored one, since it describes the terms that come first."""
    merged: dict[str, dict[str, Any]] = {k: dict(v) for k, v in (existing or {}).items()}
    for iso3, new in (incoming or {}).items():
        cur = merged.get(iso3)
        if not cur:
            merged[iso3] = new
            continue
        primary = [new.get("primary_name")] if new.get("primary_name") else None
        merged[iso3] = {
            "primary_name": cur.get("primary_name") or new.get("primary_name"),
            "formal_name": cur.get("formal_name") or new.get("formal_name"),
            "search_language": cur.get("search_language") or new.get("search_language"),
            "search_terms": union_terms(cur.get("search_terms"), new.get("search_terms"), primary),
            "excluded_terms": union_terms(cur.get("excluded_terms"), new.get("excluded_terms")),
            "antagonist_terms": union_terms(cur.get("antagonist_terms"), new.get("antagonist_terms")),
            "contrast_attributes": union_terms(cur.get("contrast_attributes"), new.get("contrast_attributes")),
            "confidence": max(float(cur.get("confidence") or 0.0), float(new.get("confidence") or 0.0)),
        }
    return merged


def only_new_countries(
    existing: dict[str, dict[str, Any]] | None, incoming: dict[str, dict[str, Any]] | None
) -> dict[str, dict[str, Any]]:
    """The part of `incoming` for countries that have no terms yet.

    Inside the window a country that already has terms keeps them: a fresh
    identification returns a slightly different list every time, and adding
    those words changed the seed the related keywords are stored under, so
    every country's Google Ads data was bought again (0,90 USD on 23/10/2026
    for a repeat analysis minutes after the first). A country nobody asked
    about before still gets its terms."""
    return {
        iso3: market
        for iso3, market in (incoming or {}).items()
        if not as_term_list(((existing or {}).get(iso3) or {}).get("search_terms"))
    }


def apply_markets(
    existing: dict[str, dict[str, Any]] | None, incoming: dict[str, dict[str, Any]], *, fixed: bool
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """(stored vocabulary afterwards, the part of `incoming` that was applied).

    Inside the window only the countries without terms are added; outside it
    everything is merged, never overwritten."""
    if fixed:
        novos = only_new_countries(existing, incoming)
        return {**(existing or {}), **novos}, novos
    return merge_markets(existing, incoming), incoming


def merge_candidates(
    existing: list[dict[str, Any]] | None, incoming: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """Merge reference-market candidates by country (union of terms), same
    accumulate-never-replace rule as the markets."""
    by_country: dict[str | None, dict[str, Any]] = {c.get("country"): dict(c) for c in (existing or [])}
    for new in incoming or []:
        cur = by_country.get(new.get("country"))
        if not cur:
            by_country[new.get("country")] = new
            continue
        primary = [new.get("primary_name")] if new.get("primary_name") else None
        cur["search_terms"] = union_terms(cur.get("search_terms"), new.get("search_terms"), primary)
        cur["excluded_terms"] = union_terms(cur.get("excluded_terms"), new.get("excluded_terms"))
        cur["primary_name"] = cur.get("primary_name") or new.get("primary_name")
        cur["search_language"] = cur.get("search_language") or new.get("search_language")
    return list(by_country.values())


def apply_candidates(
    existing: list[dict[str, Any]] | None, incoming: list[dict[str, Any]], *, fixed: bool
) -> list[dict[str, Any]]:
    """The stored candidates after an identification brought `incoming`.
    Inside the window only countries not yet among them are appended."""
    if fixed:
        conhecidos = {c.get("country") for c in (existing or [])}
        return [*(existing or []), *(c for c in incoming if c.get("country") not in conhecidos)]
    return merge_candidates(existing, incoming)
