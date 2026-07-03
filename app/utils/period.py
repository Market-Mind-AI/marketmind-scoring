import calendar
from datetime import date, timedelta

from app.utils.time import utc_now

CLOSE_MARGIN_DAYS = 5


def current_month_period_key() -> str:
    now = utc_now()
    return f"{now.year:04d}-{now.month:02d}"


def month_period_to_date_range(period_key: str) -> tuple[date, date]:
    """Slices PyTrends' continuous series (Fase 1: ``today 5-y`` collected
    once, sliced per request — see memory marketmind_core_architecture
    "Cadência de coleta") to the calendar month named by ``period_key``
    (``"YYYY-MM"``). Only MONTH is implemented — QUARTER/SEMESTER/YEAR/CUSTOM
    slicing is documented future work (Fase 4/5 orchestration doesn't need
    it yet); raise rather than silently approximate."""
    year_str, month_str = period_key.split("-")
    year, month = int(year_str), int(month_str)
    start = date(year, month, 1)
    last_day = calendar.monthrange(year, month)[1]
    end = date(year, month, last_day)
    return start, end


def is_month_closed(period_key: str, *, as_of: date | None = None) -> bool:
    """A month is closed once the following civil month starts plus a margin
    (some sources publish late) — see memory marketmind_core_architecture
    "Cadência de coleta". Once closed, a period never re-opens — callers must
    never flip an already-`is_closed=True` row back."""
    year_str, month_str = period_key.split("-")
    year, month = int(year_str), int(month_str)
    next_month_start = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    close_date = next_month_start + timedelta(days=CLOSE_MARGIN_DAYS)
    today = as_of or utc_now().date()
    return today >= close_date
