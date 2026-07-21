import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import IndicatorDimension
from app.repositories.mos_repository import MosRepository
from app.services.mos.rank_reciprocal import PRIORITY_ORDER, rank_reciprocal_weights
from app.services.weight_calibration_service import WeightCalibrationService


def _unique_period_key() -> str:
    return f"2026-07-test-{uuid.uuid4().hex[:8]}"


async def test_calibrate_writes_rank_reciprocal_weights_for_all_six_dimensions(db_session: AsyncSession) -> None:
    period_key = _unique_period_key()

    service = WeightCalibrationService(db_session)
    result = await service.calibrate(period_key=period_key)

    expected = rank_reciprocal_weights(PRIORITY_ORDER)
    assert result == {dimension.value: pytest.approx(weight) for dimension, weight in expected.items()}

    mos_repo = MosRepository(db_session)
    for dimension, weight in expected.items():
        row = await mos_repo.get_weight(dimension.value, period_key)
        assert row is not None
        assert float(row.weight) == pytest.approx(weight)


async def test_calibrate_never_overwrites_a_closed_period(db_session: AsyncSession) -> None:
    period_key = _unique_period_key()
    mos_repo = MosRepository(db_session)
    await mos_repo.upsert_weight(
        indicator_code=IndicatorDimension.DEMAND_VOLUME.value, dimension=IndicatorDimension.DEMAND_VOLUME.value,
        period_key=period_key, weight=0.99, is_closed=True,
    )  # fmt: skip
    await db_session.commit()

    service = WeightCalibrationService(db_session)
    await service.calibrate(period_key=period_key)

    row = await mos_repo.get_weight(IndicatorDimension.DEMAND_VOLUME.value, period_key)
    assert row is not None
    assert float(row.weight) == 0.99
    assert row.is_closed is True


async def test_calibrate_is_deterministic_across_periods(db_session: AsyncSession) -> None:
    period_a = _unique_period_key()
    period_b = _unique_period_key()

    service = WeightCalibrationService(db_session)
    result_a = await service.calibrate(period_key=period_a)
    result_b = await service.calibrate(period_key=period_b)

    assert result_a == result_b
