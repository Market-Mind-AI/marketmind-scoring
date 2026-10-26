import uuid

from pydantic import BaseModel, Field


class BackfillTermsResponse(BaseModel):
    entidades: int
    aliases: int
    excluidos: int
    # Termos derivados apagados por não corresponderem mais à curadoria atual
    # (ver CatalogService._podar_termos_derivados). Ficou de fora do schema
    # quando a poda entrou, e o Pydantic o descartava em silêncio — deixando
    # invisível justamente a parte destrutiva da operação.
    obsoletos: int = 0


class BackfillEmbeddingsResponse(BaseModel):
    aliases: int
    lotes: int
    # Cobrados pela Voyage nesta execução — o custo do backfill, em tokens.
    tokens: int


class BackfillAntagonistsResponse(BaseModel):
    entidades: int
    puladas: int
    falhas: int
    # Linhas gravadas/promovidas em product_excluded_terms como antagônico.
    antagonicos: int
    # O backfill de vetores que roda logo depois, no mesmo pedido.
    vetores: BackfillEmbeddingsResponse


class BackfillIdentityResponse(BaseModel):
    entidades: int
    completadas: int
    falhas: int


class BrandSplitRequest(BaseModel):
    entity_key: uuid.UUID
    brand: str = Field(..., min_length=1, description="A marca que o vocabulário da entidade realmente mede")
    generic_title: str | None = Field(
        default=None,
        description=(
            "Título do produto. Omitido, reaproveita o título da entidade — que é o caso comum, porque o "
            "título dela sempre foi o nome genérico; foi o vocabulário que virou de marca."
        ),
    )


class BrandSplitResponse(BaseModel):
    entity_key: uuid.UUID
    generic_title: str
    brand: str
    mercados_movidos: list[str]
    aliases_criados: int


class MacroPrewarmRequest(BaseModel):
    countries: list[str] | None = Field(
        default=None,
        description="ISO3 a aquecer. Omitido, aquece todos os países suportados (o caso do cron).",
    )


class MacroPrewarmResponse(BaseModel):
    paises: int
    ja_frescos: int
    coletados: int
    falhados: int


class RefreshWeightsResponse(BaseModel):
    weights: dict[str, float]
