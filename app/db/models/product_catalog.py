import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class ProductCatalog(Base):
    """Identidade de produto determinística. entity_key é sempre gerado localmente
    (uuid4), nunca bloqueado por dependência externa. A identidade
    best-effort/assíncrono, nunca requisito para a entidade existir.

    A coluna `embedding` (vector(1536)) existiu aqui até a migration 027 e foi
    removida com o passo de embedding — ver transparencia-tecnica §1.3."""

    __tablename__ = "product_catalog"

    entity_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    canonical_title: Mapped[str] = mapped_column(nullable=False)
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
    # Quando o vocabulário acima foi fixado. Dentro de `search_terms_ttl_days`
    # uma nova identificação só acrescenta países que faltam; os termos dos
    # países que já existem não mudam (migration 057).
    vocabulary_at: Mapped[datetime | None] = mapped_column(nullable=True)
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
    # SUPERADO pela migration 031 (`brand_key` + a tabela `brands`), mantido
    # enquanto for o único ponteiro de uma entidade poluída para as marcas que
    # grudaram nela — é dele que o reparo do catálogo parte. Sai depois disso.
    #
    # Marcas já vistas para este produto, acumuladas (2026-08-25). A identidade
    # do catálogo era o produto GENÉRICO — `air fryer` é a entidade, Philips e
    # Ninja são marcas dela —, então a marca não entra na decisão de identidade
    # (ver catalog_service.resolve e migration 029). Ela é acumulada aqui,
    # nunca substituída, e cada marca nova também vira um alias `marca + nome`
    # da entidade. NUNCA um alias com a marca sozinha: `philips` como alias
    # resolveria qualquer produto Philips para este, que foi exatamente o
    # estrago que a migration 028 e a poda do backfill limparam.
    brands: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    # A marca como PARTIÇÃO da identidade (migration 031). A identidade do
    # catálogo passou a ser o par (nome genérico, marca): `brand_key IS NULL` é
    # a partição genérica, e duas entidades com o mesmo nome genérico e marcas
    # diferentes são produtos diferentes.
    #
    # A marca continua NÃO entrando no texto que decide semelhança — foi
    # exatamente isso que fundiu `philips air purifier` com o alias `philips
    # airfryer` a 0.520 (ver 28ca6be). Os passos 2 e 3 comparam só o nome
    # genérico; o que a partição faz é restringir CONTRA QUEM comparar. Dentro
    # da Philips a medida volta a ser `air purifier` x `airfryer` = 0.278,
    # abaixo do limiar, e os dois produtos seguem separados.
    #
    # O que motivou: sem partição, `single malt Scotch whisky` + Cardhu e
    # `single malt Scotch whisky` + Glenfiddich resolvem para a MESMA entidade
    # pelo alias exato, e a segunda marca herda o vocabulário e a demanda da
    # primeira.
    brand_key: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("brands.brand_key"), nullable=True
    )
    # A família genérica de uma entidade de marca. É o que permite medir a
    # categoria ao lado da marca e narrar "a Cardhu é fraca aqui, mas a busca
    # por whisky é altíssima" sem misturar os dois vocabulários numa entidade
    # só (decisão D2 — o share da marca é contexto da narrativa, nunca sinal
    # pontuado). NULL numa entidade genérica: ela é a própria família.
    generic_entity_key: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("product_catalog.entity_key"), nullable=True
    )
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
        Index("ix_product_catalog_merged_into", "merged_into"),
        Index("ix_product_catalog_category", "category"),
        Index("ix_product_catalog_brand_key", "brand_key"),
        Index("ix_product_catalog_generic_entity_key", "generic_entity_key"),
        # Não existe marca sem produto genérico por trás: o LLM de
        # identificação sempre devolve o nome genérico junto (o prompt manda
        # pôr a marca só em `brand`). Ver migration 031.
        CheckConstraint(
            "brand_key IS NULL OR generic_entity_key IS NOT NULL",
            name="ck_product_catalog_brand_has_generic",
        ),
    )
