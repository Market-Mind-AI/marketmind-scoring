from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    # Every migration in alembic/versions declares datetime columns as
    # TIMESTAMP WITH TIME ZONE, but SQLAlchemy's default type inference for
    # `Mapped[datetime]` is a naive TIMESTAMP — the two silently disagreed
    # until code (app/utils/time.utc_now) started binding tz-aware Python
    # datetimes, which asyncpg then rejected against the (wrongly) inferred
    # naive column type. This makes the ORM layer match what every
    # migration already creates, for every model, without touching each one.
    type_annotation_map = {datetime: DateTime(timezone=True)}
