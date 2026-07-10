from datetime import datetime

from sqlalchemy import Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Country(Base):
    """Tabela canônica de países (ISO3 como chave), com o de-para de código de cada
    uma das 4 fontes. Construída uma vez contra dado de referência padronizado
    (ISO 3166 / UN M49) — ver marketmind_known_gaps item 6: sem LLM aqui."""

    __tablename__ = "countries"

    iso3: Mapped[str] = mapped_column(String(3), primary_key=True)
    name: Mapped[str] = mapped_column(nullable=False)
    iso2: Mapped[str | None] = mapped_column(String(2), nullable=True)
    un_numeric_code: Mapped[str | None] = mapped_column(nullable=True)
    oecd_ref_area: Mapped[str | None] = mapped_column(nullable=True)
    worldbank_country_id: Mapped[str | None] = mapped_column(nullable=True)
    undata_location_id: Mapped[str | None] = mapped_column(nullable=True)
    pytrends_geo: Mapped[str | None] = mapped_column(nullable=True)
    # Primary Google-search language (ISO 639-1) — the language a local buyer
    # searches in. compute_trend_score reads the interest_by_country row for
    # this country's term in this language (keyword localization, 2026-07-10).
    search_language: Mapped[str | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_countries_iso2", "iso2"),
        Index("ix_countries_worldbank_country_id", "worldbank_country_id"),
        Index("ix_countries_undata_location_id", "undata_location_id"),
    )
