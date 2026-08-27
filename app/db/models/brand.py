import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.utils.time import utc_now


class Brand(Base):
    """Cadastro global de marcas (migration 031).

    POR QUE TABELA PRÓPRIA. Uma marca é a mesma entidade em todos os produtos
    dela: a Philips da air fryer é a Philips do purificador de ar. Guardá-la
    repetida em cada produto (o JSONB `product_catalog.brands` da migration
    029) fazia com que "Cardhu" e "CARDHU" fossem coisas diferentes e não dava
    onde pendurar o que a marca precisa saber sobre si mesma.

    O primeiro uso concreto do cadastro é o VETO CRUZADO da demanda: as
    keywords relacionadas que o Google devolve para os termos semente da
    Cardhu incluem outras marcas, e uma keyword que carrega o nome de OUTRA
    marca cadastrada não pode entrar no volume desta. Sem o cadastro, esse
    veto não tem lista contra o que comparar.

    A marca NUNCA decide identidade sozinha, e nunca vira alias sozinha: um
    alias `philips` resolveria qualquer produto Philips para uma única
    entidade, por igualdade exata, no passo mais barato da resolução. O que a
    marca faz é PARTICIONAR — ver `ProductCatalog.brand_key`.
    """

    __tablename__ = "brands"

    brand_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Forma de superfície como vista pela primeira vez ("Cardhu"), para exibir.
    name: Mapped[str] = mapped_column(nullable=False)
    # normalize_alias(name) — "Cardhu", "CARDHU" e "cardhu" são a mesma marca.
    name_normalized: Mapped[str] = mapped_column(nullable=False)
    first_seen_source: Mapped[str | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utc_now)

    __table_args__ = (
        UniqueConstraint("name_normalized", name="uq_brands_name_normalized"),
        # Mesma guarda da migration 028: forma normalizada vazia é um ímã.
        CheckConstraint("length(trim(name_normalized)) > 0", name="ck_brands_name_normalized_not_empty"),
    )
