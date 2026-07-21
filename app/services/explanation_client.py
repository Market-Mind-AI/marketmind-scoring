"""Fase 5 — final LLM inference. Assembles a deterministic "evidence packet"
from the computed MOS + indicator values and asks the marketmind-ollma-llm
service (a local Ollama qwen3:14b) to turn it into a business-facing narrative
of the market-opportunity ranking ("where to sell").

Best-effort: any failure (service down, timeout, LLM error) returns None and is
logged — the analysis still completes with the numbers, just without prose. The
LLM only EXPLAINS these deterministic values; it never computes or alters a
score (Cap. 4 §4.0, memory marketmind_mos_engine).
"""

import uuid
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import logger
from app.db.models.mos_result import MosResult
from app.domain.enums import IndicatorCode
from app.repositories.indicator_repository import IndicatorRepository
from app.services.mos.dimension_mapping import DIMENSION_BY_INDICATOR, compute_dimension_subscores
from app.services.mos.interpretation import INDICATOR_MEANING


def _label(indicator_code: str) -> str:
    """Readable name from an indicator code, e.g. trend_score -> 'Demand Volume'."""
    special = {
        "trend_score": "Demand Volume",
        "growth_score": "Demand Growth",
        "demand_stability_score": "Demand Stability",
        "search_intent_score": "Purchase Intent",
    }
    if indicator_code in special:
        return special[indicator_code]
    return indicator_code.removesuffix("_score").replace("_", " ").title()


class ExplanationClient:
    """Builds the evidence packet and calls the final-inference LLM service."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._indicator_repo = IndicatorRepository(session)

    async def _country_evidence(
        self, entity_key: uuid.UUID, mos_result: MosResult, period_key: str
    ) -> dict[str, Any]:
        country = mos_result.country
        country_values = await self._indicator_repo.get_values_for(
            entity_key=None, country=country, period_type="month", period_key=period_key
        )
        product_values = await self._indicator_repo.get_values_for(
            entity_key=entity_key, country=country, period_type="month", period_key=period_key
        )
        merged = {**country_values, **product_values}

        signals: list[dict[str, Any]] = []
        for code in DIMENSION_BY_INDICATOR:
            iv = merged.get(code.value)
            if iv is None or iv.normalized_score is None:
                continue
            # Keep the packet compact so it never overflows the local model's
            # context (truncation makes qwen3 confabulate/drop countries): carry
            # the verbose meaning + the concrete numbers ONLY on the demand-volume
            # signal (the one that needs the volume/population/penetration
            # narrative); the rest are just code/label/score/band.
            is_volume = code == IndicatorCode.TREND_SCORE
            signals.append(
                {
                    "code": code.value,
                    "label": _label(code.value),
                    "score": round(float(iv.normalized_score), 1),
                    "band": iv.interpretation_band,
                    "meaning": INDICATOR_MEANING.get(code) if is_volume else None,
                    "detail": iv.notes if is_volume else None,
                }
            )
        dimensions = {dim.value: round(score, 1) for dim, score in compute_dimension_subscores(merged).items()}
        return {
            "country": country,
            "mos": round(float(mos_result.mos), 1) if mos_result.mos is not None else None,
            "mos_band": mos_result.mos_interpretation,
            "dimensions": dimensions,
            "signals": signals,
        }

    async def generate(
        self, *, entity_key: uuid.UUID, product_name: str, mos_results: list[MosResult], period_key: str
    ) -> str | None:
        scored = [r for r in mos_results if r.mos is not None]
        if not scored:
            return None
        try:
            countries = [await self._country_evidence(entity_key, r, period_key) for r in scored]
            payload = {
                "product_name": product_name,
                "language": settings.ollama_llm_narrative_language,
                "countries": countries,
            }
            async with httpx.AsyncClient(
                base_url=settings.ollama_llm_api_base_url, timeout=settings.ollama_llm_timeout_seconds
            ) as client:
                response = await client.post("/explain", json=payload)
                response.raise_for_status()
                narrative = response.json().get("narrative")
            if not narrative:
                logger.warning("Explanation service returned an empty narrative")
                return None
            return str(narrative)
        except Exception as exc:  # noqa: BLE001 — best-effort enrichment, never fails the analysis
            logger.warning(f"Final LLM explanation skipped (service unavailable or errored): {exc}")
            return None
