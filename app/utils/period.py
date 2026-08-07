import calendar
from datetime import date, timedelta

from app.utils.time import utc_now

CLOSE_MARGIN_DAYS = 5

# Sentinel period_key for country-level indicators (WorldBank, OECD, UNData).
#
# These are not a monthly series. Every one of them is read as "the most recent
# observation" — the indicator functions query the raw tables with
# ``ORDER BY year DESC LIMIT 1`` (or an average of the last k), and never filter
# by the coverage row's period_key. Coverage here answers one question only:
# "have we downloaded this country's macro data recently enough?"
#
# It used to be keyed by calendar month, which made that question unanswerable:
# a lookup for the new month found no row at all, so `is_fresh` short-circuited
# to False before the TTL was ever consulted, and every country's whole macro
# set was re-collected on the first analysis of each month. The stored data
# proved the waste — 12 indicators × 6 monthly period_keys for one country, all
# fetched in the same minute, every month holding the identical value.
#
# With one key, the TTL is what decides, which is what it was always meant to do.
COUNTRY_LEVEL_PERIOD_KEY = "LATEST"


def current_month_period_key() -> str:
    now = utc_now()
    return f"{now.year:04d}-{now.month:02d}"


def last_n_month_period_keys(n: int) -> list[str]:
    """Fase 5 (POST /analyze) — the last `n` calendar months up to and
    including the current one, most recent first (e.g. n=6 from 2026-07 ->
    ["2026-07", "2026-06", ..., "2026-02"]). Used as the data-freshness
    window: ensure coverage for each of these period_keys, not a request to
    compute/return a MOS per month (the final answer is still the current
    month's MOS — see marketmind_mos_engine)."""
    if n < 1:
        raise ValueError("n must be >= 1")
    now = utc_now()
    year, month = now.year, now.month
    keys = []
    for _ in range(n):
        keys.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return keys


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
