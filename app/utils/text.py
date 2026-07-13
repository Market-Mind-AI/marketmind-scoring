import re
import unicodedata
from collections.abc import Mapping, Sequence


def normalize_alias(text: str) -> str:
    """Deterministic text normalization for product_aliases.alias_normalized
    (catalog_service pipeline step 1 — exact alias lookup). Same input text
    must always normalize to the same key: lowercase, accents stripped,
    punctuation removed, whitespace collapsed."""
    decomposed = unicodedata.normalize("NFKD", text)
    without_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    lowered = without_accents.lower()
    alnum_only = re.sub(r"[^a-z0-9\s]", " ", lowered)
    return re.sub(r"\s+", " ", alnum_only).strip()


def as_term_list(value: object) -> list[str]:
    """Coerce a localized_terms value to a clean list of terms, tolerating the
    legacy `{lang: "term"}` (single string) shape alongside `{lang: [terms]}`."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, Sequence):
        return [t for t in value if isinstance(t, str) and t.strip()]
    return []


def merge_localized_terms(
    existing: Mapping[str, object] | None, new: Mapping[str, object] | None
) -> dict[str, list[str]]:
    """Union two `{lang: [term variations]}` maps into an accumulating acervo,
    deduping per language by `normalize_alias` (case/accent-insensitive) while
    preserving the first-seen surface form and order (existing terms first,
    then any genuinely new ones). Tolerates the legacy single-string shape.
    Pure — callers persist the result. See marketmind_mos_engine (keyword
    localization by data arbitration)."""
    merged: dict[str, list[str]] = {}
    for source in (existing or {}), (new or {}):
        for language, raw in source.items():
            bucket = merged.setdefault(language, [])
            seen = {normalize_alias(t) for t in bucket}
            for term in as_term_list(raw):
                key = normalize_alias(term)
                if key and key not in seen:
                    seen.add(key)
                    bucket.append(term)
    return {language: terms for language, terms in merged.items() if terms}
