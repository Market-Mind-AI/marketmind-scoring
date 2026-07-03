from datetime import UTC, datetime


def utc_now() -> datetime:
    """Timezone-aware, unlike the ``datetime.utcnow`` default used by most
    ORM columns in this codebase (all of which are ``DateTime(timezone=True)``
    at the DB level regardless). Values read back from Postgres for those
    columns come back tz-aware (asyncpg), so any code that computes against
    "now" — not just writes it — must use this, not a naive helper, to avoid
    ``TypeError: can't compare offset-naive and offset-aware datetimes``."""
    return datetime.now(UTC)


def elapsed_ms(start: datetime) -> int:
    delta = utc_now() - start
    return int(delta.total_seconds() * 1000)
