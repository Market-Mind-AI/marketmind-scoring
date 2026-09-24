from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import EntityNotFoundError, MarketMindCoreError
from app.db.session import get_session
from app.schemas.admin import (
    BackfillTermsResponse,
    BrandSplitRequest,
    BrandSplitResponse,
    MacroPrewarmRequest,
    MacroPrewarmResponse,
    RefreshWeightsResponse,
)
from app.services.brand_split_service import BrandSplitService
from app.services.catalog_service import CatalogService
from app.services.macro_coverage import MacroPrewarmService
from app.services.weight_calibration_service import WeightCalibrationService
from app.utils.period import current_month_period_key

router = APIRouter(prefix="/admin", tags=["Admin"])


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
    "/catalog/split-brand",
    response_model=BrandSplitResponse,
    summary="Separa uma entidade genérica cujo vocabulário é de uma marca",
    description=(
        "Conserta o estado legado que a partição da identidade (migration 031) impede de acontecer de "
        "novo mas não desfaz: uma entidade com título genérico e vocabulário de uma marca só. Produz três "
        "vocabulário: a entidade CONTINUA sendo o produto e os termos curados vão para a marca, "
        "chaveados por (produto, marca, país). Não cria entidade nova, não aposenta ninguém e não "
        "recoleta nada — as linhas de demanda seguem em cache no coletor. O campeão é apagado: ele foi "
        "eleito com a demanda da marca e não vale para o produto. "
        "Ver app/services/brand_split_service.py."
    ),
)
async def split_brand(request: BrandSplitRequest, session: AsyncSession = Depends(get_session)) -> BrandSplitResponse:
    try:
        report = await BrandSplitService(session).split(
            entity_key=request.entity_key, brand=request.brand, generic_title=request.generic_title
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except MarketMindCoreError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return BrandSplitResponse(**asdict(report))


@router.post(
    "/coverage/prewarm-macro",
    response_model=MacroPrewarmResponse,
    summary="Mantém o dado macro dos países quente, fora de qualquer análise",
    description=(
        "Coleta o que estiver vencido de World Bank, OECD e UN Data para os países suportados. Os "
        "indicadores macro são anuais, lidos como último valor conhecido e válidos por 90 dias — não têm "
        "relação com o produto analisado nem com o instante da consulta —, mas eram coletados DENTRO da "
        "análise, o que amarrava o resultado do usuário à fonte estar de pé naquele minuto. Em 25/08/2026 "
        "a API do UN Data ficou 29 minutos fora e 9 de 24 países saíram do ranking. Sem agendador em "
        "processo: feito para um cron. Não substitui o retry da análise, reduz a frequência com que ele "
        "precisa agir."
    ),
)
async def prewarm_macro(
    request: MacroPrewarmRequest | None = None, session: AsyncSession = Depends(get_session)
) -> MacroPrewarmResponse:
    relatorio = await MacroPrewarmService(session).prewarm(countries=request.countries if request else None)
    return MacroPrewarmResponse(**relatorio)


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
