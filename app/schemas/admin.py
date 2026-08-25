from pydantic import BaseModel


class EnrichmentReportResponse(BaseModel):
    checked: int
    enriched: int
    no_match: int
    failed: int


class BackfillTermsResponse(BaseModel):
    entidades: int
    aliases: int
    excluidos: int


class MergeReportResponse(BaseModel):
    merged_pairs: int


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


class RefreshWeightsResponse(BaseModel):
    weights: dict[str, float]
