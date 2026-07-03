import re
import unicodedata


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
