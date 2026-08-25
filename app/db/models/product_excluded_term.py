import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.utils.time import utc_now


class ProductExcludedTerm(Base):
    """Evidência NEGATIVA de identidade: os termos que o catálogo declara que
    NÃO são este produto (`localized_markets[<ISO3>].excluded_terms`).

    POR QUE ISTO EXISTE. `excluded_terms` era curado desde a Fase 5 e servia só
    para filtrar volume de busca. A decisão de identidade usava exclusivamente
    evidência positiva — e evidência positiva não separa produtos opostos,
    porque opostos compartilham quase toda a forma da palavra. Medido contra o
    catálogo real (2026-08-25, 9 entidades, 286 aliases, 102 termos excluídos),
    com os termos localizados indexados e o limiar 0.4 configurado:

        0.765  'öl fritteuse'        -> air fryer  via 'fritteuse ohne öl'
        0.727  'fritadeira a óleo'   -> air fryer  via 'fritadeira sem óleo'
        0.696  'fritadeira de óleo'  -> air fryer  via 'fritadeira sem óleo'
        0.696  'freidora de aceite'  -> air fryer  via 'freidora sin aceite'
        0.667  'oil fryer'           -> air fryer  via 'oil less fryer'

    44 dos 102 termos que o catálogo declara como "isto NÃO é este produto"
    (43,1%) resolviam para a própria entidade que os exclui, gravando alias
    exato permanente. Com o veto do passo 2 (ver ProductRepository.trigram_search)
    a taxa vai a 0/102, sem perder nenhum positivo (284/284); numa consulta inédita o recall vai de 94,7% para 93,0%.

    A regra não introduz limiar novo: compara a proximidade do texto com o que
    a entidade DIZ QUE É contra a proximidade com o que ela DIZ QUE NÃO É, e a
    evidência mais próxima ganha. Margens de folga foram medidas (0.05 a 0.20)
    e pioram o trade — a precisão anda 3 pontos enquanto o recall cai de 93%
    para 77%.

    NUNCA é porta de entrada: um termo excluído não vira alias e não resolve
    nada sozinho. Só desqualifica candidato.
    """

    __tablename__ = "product_excluded_terms"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entity_key: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("product_catalog.entity_key"), nullable=False
    )
    # Mesma normalização dos aliases (app.utils.text.normalize_alias), para que
    # o similarity() do pg_trgm compare os dois lados na mesma régua.
    term_normalized: Mapped[str] = mapped_column(nullable=False)
    # Forma de superfície, preservada só para log/auditoria: é o que o curador
    # (ou o LLM de identificação) escreveu, com acento e pontuação.
    term: Mapped[str] = mapped_column(nullable=False)
    source: Mapped[str] = mapped_column(nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(nullable=False, default=utc_now)

    __table_args__ = (
        # Por entidade, não global: duas entidades podem legitimamente excluir
        # o mesmo termo (`forno elétrico` não é air fryer nem panela de pressão).
        UniqueConstraint("entity_key", "term_normalized", name="uq_product_excluded_terms_entity_term"),
        Index("ix_product_excluded_terms_entity_key", "entity_key"),
        Index(
            "ix_product_excluded_terms_term_normalized_trgm",
            "term_normalized",
            postgresql_using="gin",
            postgresql_ops={"term_normalized": "gin_trgm_ops"},
        ),
    )
