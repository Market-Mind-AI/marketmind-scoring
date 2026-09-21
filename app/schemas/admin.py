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


class BrandSplitRequest(BaseModel):
    entity_key: uuid.UUID
    brand: str = Field(..., min_length=1, description="A marca que o vocabulário da entidade realmente mede")
    generic_title: str | None = Field(
        default=None,
        description=(
            "Título da família genérica. Omitido, reaproveita o título da entidade poluída — que é o caso "
            "comum, porque o título dela sempre foi o nome genérico; foi o vocabulário que virou de marca."
        ),
    )


class BrandSplitResponse(BaseModel):
    brand_entity_key: uuid.UUID
    generic_entity_key: uuid.UUID
    retired_entity_key: uuid.UUID
    brand: str
    brand_title: str
    generic_title: str
    aliases_para_marca: int
    aliases_para_familia: int
    termos_excluidos_movidos: int
    mercados_movidos: list[str]


class ReconciliationDriftItem(BaseModel):
    coverage_index_id: str
    source: str
    entity_key: str | None
    country: str
    indicator_code: str
    period_key: str
    reason: str


class ReconciliationReportResponse(BaseModel):
    checked: int
    drift: list[ReconciliationDriftItem]
    unmappable: list[ReconciliationDriftItem]


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
