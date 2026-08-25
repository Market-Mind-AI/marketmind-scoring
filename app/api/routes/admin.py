from dataclasses import asdict

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.schemas.admin import (
    BackfillTermsResponse,
    EnrichmentReportResponse,
    MergeReportResponse,
    ReconciliationReportResponse,
    RefreshWeightsResponse,
)
from app.services.catalog_service import CatalogService
from app.services.enrichment_service import EnrichmentService
from app.services.merge_service import MergeService
from app.services.reconciliation_service import ReconciliationService
from app.services.weight_calibration_service import WeightCalibrationService
from app.utils.period import current_month_period_key

router = APIRouter(prefix="/admin", tags=["Admin"])


@router.post(
    "/reconciliation/run",
    response_model=ReconciliationReportResponse,
    summary="Run coverage reconciliation",
    description=(
        "Compares coverage_index against the 4 services' raw observation "
        "tables to catch drift. Low-priority/diagnostic — meant to be "
        "invoked periodically by an external cron/systemd timer, no "
        "in-process scheduler exists yet. See app/services/reconciliation_service.py "
        "for the exact scope (WorldBank/OECD/UNData only for Fase 1)."
    ),
)
async def run_reconciliation(session: AsyncSession = Depends(get_session)) -> ReconciliationReportResponse:
    report = await ReconciliationService(session).reconcile()
    return ReconciliationReportResponse(
        checked=report.checked,
        drift=[asdict(d) for d in report.drift],
        unmappable=[asdict(d) for d in report.unmappable],
    )


@router.post(
    "/catalog/enrich-mid",
    response_model=EnrichmentReportResponse,
    summary="Best-effort google_mid enrichment",
    description=(
        "For up to `limit` entities missing google_mid: asks PyTrends for "
        "suggestions on canonical_title and picks the best deterministic "
        "match. Never blocks catalog resolution — see app/services/enrichment_service.py."
    ),
)
async def enrich_mid(limit: int = 20, session: AsyncSession = Depends(get_session)) -> EnrichmentReportResponse:
    report = await EnrichmentService(session).enrich_missing_mids(limit=limit)
    return EnrichmentReportResponse(
        checked=report.checked, enriched=report.enriched, no_match=report.no_match, failed=report.failed
    )


@router.post(
    "/catalog/backfill-terms",
    response_model=BackfillTermsResponse,
    summary="Index the curated vocabulary of existing catalog entities",
    description=(
        "Indexes every entity's `search_terms` as aliases and its "
        "`excluded_terms` as negative evidence. Both only run inside "
        "`resolve()`, so entities created before those features have neither. "
        "The two sides are always filled together: indexing only the positive "
        "terms turns on multilingual recall AND the merges the negative "
        "evidence exists to block. See app/db/models/product_excluded_term.py."
    ),
)
async def backfill_terms(session: AsyncSession = Depends(get_session)) -> BackfillTermsResponse:
    relatorio = await CatalogService(session).backfill_terms()
    return BackfillTermsResponse(**relatorio)


@router.post(
    "/catalog/merge",
    response_model=MergeReportResponse,
    summary="Merge duplicate catalog entities",
    description=(
        "Merges entities discovered to share the same (enriched) google_mid. "
        "Run after /admin/catalog/enrich-mid — see app/services/merge_service.py."
    ),
)
async def merge_catalog(session: AsyncSession = Depends(get_session)) -> MergeReportResponse:
    report = await MergeService(session).merge_duplicates()
    return MergeReportResponse(merged_pairs=report.merged_pairs)


@router.post(
    "/mos/refresh-weights",
    response_model=RefreshWeightsResponse,
    summary="Calibrate MOS dimension weights (Rank Reciprocal)",
    description=(
        "Writes all 6 dimension weights for the given period using Rank "
        "Reciprocal (RR) over an explicit priority order (Demand > Macro "
        "Demand Structure > Market Size > Economic Readiness > Digital "
        "Readiness > Demographic Sustainability) — Rank Order Centroid "
        "(ROC) was used from 2026-07-03 (replacing Entropy Weight, which "
        "live testing showed repeatedly undervaluing Demand) until "
        "2026-07-08, when adding the Macro Demand Structure dimension "
        "prompted switching to RR to narrow the Market Size / Economic "
        "Readiness gap without hand-picking weights. Weights are fixed (no "
        "indicator data is read), but still versioned by period_key and "
        "never overwrite an already-closed period, consistent with "
        "normalization_reference. See app/services/mos/rank_reciprocal.py."
    ),
)
async def refresh_weights(
    period_key: str | None = None, session: AsyncSession = Depends(get_session)
) -> RefreshWeightsResponse:
    resolved_period_key = period_key or current_month_period_key()
    weights = await WeightCalibrationService(session).calibrate(period_key=resolved_period_key)
    return RefreshWeightsResponse(weights=weights)
