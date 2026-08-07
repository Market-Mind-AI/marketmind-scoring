"""TLS configuration for the Postgres connections.

Why this exists: Azure Database for PostgreSQL Flexible Server ships with
``require_secure_transport = ON``, so the plain ``postgresql+asyncpg://...``
DSN this service builds locally is refused outright by the server. Locally
(docker-compose, ``pgvector/pgvector:pg16``) there is no TLS listener at all,
which is why ``disable`` is the default — enabling TLS is a deploy-time
decision, not a code one, and the same image has to work in both places.

Two translations are needed, because the two drivers disagree:

- **asyncpg** (the async engines) does *not* understand libpq's ``sslmode``
  query parameter. Appending ``?sslmode=require`` to an asyncpg URL raises
  ``TypeError: connect() got an unexpected keyword argument 'sslmode'`` rather
  than enabling TLS. It takes an ``ssl`` connect arg instead.
- **psycopg2** (used by Alembic through the sync DSN) *does* understand
  ``sslmode`` natively, and takes it as a query string.

Modes mirror libpq's vocabulary so the env var reads the way a Postgres
operator expects:

``disable``      no TLS (local docker-compose)
``require``      encrypt, but do not validate the server certificate
``verify-full``  encrypt and validate certificate + hostname
"""

import ssl

DISABLE = "disable"
REQUIRE = "require"
VERIFY_FULL = "verify-full"

_VALID_MODES = frozenset({DISABLE, REQUIRE, VERIFY_FULL})


def _validated(mode: str) -> str:
    normalized = (mode or DISABLE).strip().lower()
    if normalized not in _VALID_MODES:
        raise ValueError(
            f"Invalid DATABASE_SSL_MODE {mode!r}. Expected one of: {', '.join(sorted(_VALID_MODES))}"
        )
    return normalized


def asyncpg_ssl_connect_args(mode: str) -> dict[str, object]:
    """Build the ``connect_args`` dict for ``create_async_engine``.

    Returns an empty dict for ``disable`` so the local setup keeps the exact
    connection behaviour it had before this module existed.
    """
    normalized = _validated(mode)

    if normalized == DISABLE:
        return {}

    if normalized == REQUIRE:
        # libpq's `require` encrypts without authenticating the server. Python's
        # default context does the opposite, so both checks are turned off
        # explicitly — and in this order, because setting `verify_mode` to
        # CERT_NONE while `check_hostname` is still True raises ValueError.
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return {"ssl": context}

    # verify-full: the system CA bundle in the python:3.12-slim base image
    # already chains to the DigiCert root that signs Azure's Postgres
    # certificates, so no custom CA file is needed.
    return {"ssl": ssl.create_default_context()}


def sslmode_query_suffix(mode: str) -> str:
    """Build the query-string suffix for a psycopg2 (sync) DSN.

    Empty string for ``disable``, so the DSN is byte-identical to what it was
    before TLS was configurable.
    """
    normalized = _validated(mode)
    if normalized == DISABLE:
        return ""
    return f"?sslmode={normalized}"
