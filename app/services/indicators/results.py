from dataclasses import dataclass
from decimal import Decimal

from app.domain.enums import AvailabilityStatus, IndicatorDirection


@dataclass
class RawIndicatorResult:
    """Output of a single Cap. 4 raw-value function — before normalization
    (see app/services/indicators/normalization.py). ``raw_value=None`` always
    pairs with ``availability_status in (UNAVAILABLE, CONDITIONAL)``."""

    raw_value: Decimal | float | None
    direction: IndicatorDirection
    calculation_window: str
    availability_status: AvailabilityStatus
    notes: str | None = None
