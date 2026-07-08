from datetime import UTC, date, datetime
from unittest.mock import patch

import pytest

from app.utils.period import is_month_closed, last_n_month_period_keys, month_period_to_date_range


def test_month_period_to_date_range_regular_month() -> None:
    start, end = month_period_to_date_range("2026-02")
    assert start == date(2026, 2, 1)
    assert end == date(2026, 2, 28)


def test_month_period_to_date_range_leap_year_february() -> None:
    start, end = month_period_to_date_range("2024-02")
    assert start == date(2024, 2, 1)
    assert end == date(2024, 2, 29)


def test_month_period_to_date_range_december_does_not_overflow_year() -> None:
    start, end = month_period_to_date_range("2026-12")
    assert start == date(2026, 12, 1)
    assert end == date(2026, 12, 31)


def test_is_month_closed_true_after_margin() -> None:
    # July 2026 closes on 2026-08-06 (next month start + 5 days).
    assert is_month_closed("2026-07", as_of=date(2026, 8, 6)) is True
    assert is_month_closed("2026-07", as_of=date(2026, 8, 10)) is True


def test_is_month_closed_false_before_margin() -> None:
    assert is_month_closed("2026-07", as_of=date(2026, 8, 5)) is False
    assert is_month_closed("2026-07", as_of=date(2026, 7, 31)) is False


def test_is_month_closed_across_year_boundary() -> None:
    assert is_month_closed("2026-12", as_of=date(2027, 1, 5)) is False
    assert is_month_closed("2026-12", as_of=date(2027, 1, 6)) is True


def test_last_n_month_period_keys_regular_case() -> None:
    with patch("app.utils.period.utc_now", return_value=datetime(2026, 7, 8, tzinfo=UTC)):
        assert last_n_month_period_keys(6) == [
            "2026-07", "2026-06", "2026-05", "2026-04", "2026-03", "2026-02",
        ]  # fmt: skip


def test_last_n_month_period_keys_crosses_year_boundary() -> None:
    with patch("app.utils.period.utc_now", return_value=datetime(2026, 2, 1, tzinfo=UTC)):
        assert last_n_month_period_keys(6) == [
            "2026-02", "2026-01", "2025-12", "2025-11", "2025-10", "2025-09",
        ]  # fmt: skip


def test_last_n_month_period_keys_n_equals_one() -> None:
    with patch("app.utils.period.utc_now", return_value=datetime(2026, 7, 8, tzinfo=UTC)):
        assert last_n_month_period_keys(1) == ["2026-07"]


def test_last_n_month_period_keys_rejects_non_positive_n() -> None:
    with pytest.raises(ValueError, match="n must be >= 1"):
        last_n_month_period_keys(0)
