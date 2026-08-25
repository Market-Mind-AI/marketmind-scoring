import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import ForeignKey, Index, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class ProductCatalog(Base):
    """Identidade de produto determinística. entity_key é sempre gerado localmente
    (uuid4), nunca bloqueado por dependência externa. google_mid é enriquecimento
    best-effort/assíncrono, nunca requisito para a entidade existir.

    A coluna `embedding` (vector(1536)) existiu aqui até a migration 027 e foi
    removida com o passo de embedding — ver transparencia-tecnica §1.3."""

    __tablename__ = "product_catalog"

    entity_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    canonical_title: Mapped[str] = mapped_column(nullable=False)
    google_mid: Mapped[str | None] = mapped_column(nullable=True)
    google_type: Mapped[str | None] = mapped_column(nullable=True)
    category: Mapped[str | None] = mapped_column(nullable=True)
    # Per-COUNTRY curated search terms (2026-07-15), keyed by ISO3:
    #   {"<ISO3>": {"primary_name", "formal_name"?, "search_terms": [...],
    #               "excluded_terms": [...], "confidence": float}}
    # Produced by the identify-image LLM (image + target countries) and
    # user-reviewable. The demand LEVEL = SUM of the absolute Google Ads search
    # volumes of a country's `search_terms` (deduped); `excluded_terms` filter
    # ambiguous collisions; `search_terms` also seed the related-keyword /
    # intent call. Replaces the old Trends-era {lang: [names]} `localized_terms`.
    # See marketmind_mos_engine.
    localized_markets: Mapped[dict[str, dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    # reference_candidates (2026-07-22): a RANKED shortlist (up to 5) of the
    # supported countries most likely to be this product's biggest market,
    # proposed by the enrichment LLM, each a localized-market entry plus a
    # "country" (ISO3):
    #   [{"country": "IND", "primary_name": ..., "search_terms": [...],
    #     "excluded_terms": [...], "confidence": float}, ...]
    # The analysis collects each candidate's real search volume (reusing cached
    # data) and the LARGEST becomes the CEILING of the Demand Volume ruler
    # (Cap. 4 §4.2.1): trend_score = country_volume / ceiling x 100. Real data
    # decides the biggest — a wrong single LLM guess can no longer skew the
    # ruler. Replaces the old fixed 2M ceiling. The winning reference is never
    # scored/ranked itself — context only, explained by the final LLM. See
    # marketmind_mos_engine.
    reference_candidates: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    target_age_min: Mapped[int | None] = mapped_column(nullable=True)
    target_age_max: Mapped[int | None] = mapped_column(nullable=True)

    # The product's standout reference market, cached here because it is a
    # property of the product and not of any one request: MOS uses fixed 100/0
    # TOPSIS anchors, so a candidate scores the same whoever asked and whatever
    # was asked alongside it.
    #
    # Recomputing it per analysis meant re-collecting candidate demand from
    # PyTrends, the slowest thing in the system, for a result that does not
    # change. champion_period_key is what makes it stale — demand is collected
    # per month, so a champion is valid for the period it was computed in.
    #
    # champion_period_key NULL means "never computed"; a non-NULL period with a
    # NULL champion_country means "computed for that period and no candidate
    # could be scored", which must not be retried on every analysis.
    champion_country: Mapped[str | None] = mapped_column(String(3), nullable=True)
    champion_mos: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    champion_period_key: Mapped[str | None] = mapped_column(nullable=True)
    champion_computed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    merged_into: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("product_catalog.entity_key"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=datetime.utcnow)

    aliases: Mapped[list["ProductAlias"]] = relationship(  # type: ignore[name-defined]  # noqa: F821
        back_populates="product", foreign_keys="ProductAlias.entity_key"
    )

    __table_args__ = (
        Index("ix_product_catalog_google_mid", "google_mid"),
        Index("ix_product_catalog_merged_into", "merged_into"),
        Index("ix_product_catalog_category", "category"),
    )
