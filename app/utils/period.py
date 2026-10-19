from app.utils.time import utc_now

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
