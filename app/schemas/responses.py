from pydantic import BaseModel


class ExternalSourceStatus(BaseModel):
    source: str
    status: str


class HealthResponse(BaseModel):
    status: str
    database: str
    external_sources: list[ExternalSourceStatus]
    app_name: str
    environment: str
