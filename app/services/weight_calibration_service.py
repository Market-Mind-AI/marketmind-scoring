"""Fase 4: calibrates the 6 dimension weights using Rank Reciprocal (RR),
not Entropy Weight and no longer Rank Order Centroid (ROC) either — see
app/services/mos/rank_reciprocal.py for the full rationale (2026-07-03
decision replacing Entropy Weight after live testing showed it repeatedly
undervaluing Demand; 2026-07-08 decision replacing ROC with RR after adding
the 6th dimension). Weights are fixed (derived from an explicit priority
order, not from any indicator data) but still versioned by period_key and
locked by is_closed, consistent with normalization_reference and every
other periodically-versioned table in this schema — a future change to the
priority order only affects periods opened after that change, never
retroactively.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.mos_repository import MosRepository
from app.services.mos.rank_reciprocal import PRIORITY_ORDER, rank_reciprocal_weights


class WeightCalibrationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._mos_repo = MosRepository(session)

    async def calibrate(self, *, period_key: str) -> dict[str, float]:
        weights = rank_reciprocal_weights(PRIORITY_ORDER)
        for dimension, weight in weights.items():
            await self._mos_repo.upsert_weight(
                indicator_code=dimension.value,
                dimension=dimension.value,
                period_key=period_key,
                weight=weight,
                is_closed=False,
            )
        await self._session.commit()
        return {dimension.value: weight for dimension, weight in weights.items()}
