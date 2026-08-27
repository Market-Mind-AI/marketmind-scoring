import uuid
from typing import Any

from pydantic import BaseModel, Field


class AnalyzeRequest(BaseModel):
    # Same field names as IdentifyImageResponse (app/schemas/catalog.py) on
    # purpose — the frontend passes these through unchanged, with whatever
    # the user corrected, from POST /catalog/identify-image.
    product_name: str = Field(..., min_length=1)
    brand: str | None = None
    category: str | None = None
    localized_markets: dict[str, dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Per-COUNTRY curated search vocabulary from identify-image, keyed by ISO3 "
            '({"<ISO3>": {primary_name, formal_name?, search_terms, excluded_terms, confidence}}).'
        ),
    )
    reference_candidates: list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Ranked shortlist of candidate biggest-markets from identify-image "
            "([{country, primary_name, search_terms, excluded_terms, confidence}, ...]) — the largest by real "
            "search volume becomes the Demand Volume ruler ceiling."
        ),
    )
    target_age_min: int | None = None
    target_age_max: int | None = None
    estimated_price: float | None = None
    price_currency: str | None = None
    user_prompt: str | None = Field(
        default=None,
        description=(
            "The buyer's own free-text description/question (the same text used as "
            "the identify-image hint). Stored and passed to the final explanation "
            "LLM so it can answer what the user asked — strictly from the "
            "deterministic evidence, never inventing data."
        ),
    )
    countries: list[str] = Field(..., min_length=1, description="ISO3 country codes")


class AnalyzeAcceptedResponse(BaseModel):
    request_id: uuid.UUID
    status: str  # "pending"


class AnalysisStatusResponse(BaseModel):
    request_id: uuid.UUID
    status: str  # "pending" | "running" | "done" | "failed"
    # Com marca, canonical_title carrega a marca ("Cardhu single malt Scotch
    # whisky"). `brand` e `category_title` vêm separados, lidos da entidade, para
    # o relatório poder dizer "Cardhu · single malt Scotch whisky" e para a
    # leitura do contexto de categoria (o sinal `category_volume_score` do
    # breakdown) ter nome. Nulos numa análise de produto genérico.
    canonical_title: str | None
    brand: str | None = None
    category_title: str | None = None
    user_prompt: str | None = None  # the buyer's free-text prompt/question, echoed back for the UI
    result: list[dict[str, Any]] | None
    explanation: str | None = None  # LLM narrative of the ranking (best-effort; null if unavailable)
    # Full per-country evidence packet (glossary + weights + demand ruler + each
    # country's MOS, dimension subscores and scored signals with explanations) —
    # what the frontend renders in the rich MOS modal. Rebuilt lazily on read for
    # analyses stored before this field existed; null only if nothing was scored.
    breakdown: dict[str, Any] | None = None
    error_message: str | None
    # No que a análise está esperando AGORA. Uma análise que insiste numa fonte
    # fora do ar é legítima e pode levar horas — sem esta frase, o frontend não
    # tem como distingui-la de uma travada. Nulo quando não está esperando nada.
    progress: str | None = None
