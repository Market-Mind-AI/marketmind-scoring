from datetime import date

from app.utils.period import is_month_closed, month_period_to_date_range


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
