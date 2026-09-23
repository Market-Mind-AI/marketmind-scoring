"""Every instruction kept in Langfuse Prompt Management, by name, with its text
in code — the fallback at runtime and the seed for scripts/sync_prompts.py.

Its own module so that app/prompts.py never imports the service modules."""

from app.services.catalog_matcher import _SYSTEM as _CATALOG_MATCH_SYSTEM
from app.services.enrichment_client import _SYSTEM as _ENRICHMENT_SYSTEM
from app.services.explanation_client import _SYSTEM_PROMPT_EN, _SYSTEM_PROMPT_PT
from app.services.reference_market_client import _SYSTEM as _REFERENCE_MARKET_SYSTEM
from app.services.vision_client import _INSTRUCTIONS as _VISION_INSTRUCTIONS

PROMPTS: dict[str, str] = {
    "catalog-match-system": _CATALOG_MATCH_SYSTEM,
    "enrichment-system": _ENRICHMENT_SYSTEM,
    "reference-market-system": _REFERENCE_MARKET_SYSTEM,
    "vision-instructions": _VISION_INSTRUCTIONS,
    "explanation-system-pt": _SYSTEM_PROMPT_PT,
    "explanation-system-en": _SYSTEM_PROMPT_EN,
}
