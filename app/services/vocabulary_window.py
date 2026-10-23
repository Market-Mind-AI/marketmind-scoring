"""How long a product's search vocabulary stays fixed.

One rule, read by the two places that store search terms: the product's own
vocabulary (`product_catalog.localized_markets`) and a brand's
(`brand_markets`). See `search_terms_ttl_days` for why it exists.
"""

from datetime import UTC, datetime, timedelta

from app.core.config import settings
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
