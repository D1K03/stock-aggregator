"""The four momentum metrics (spec D5).

Calendar months, not session counts: that survives holidays without a market
calendar, and "three-month return" means the calendar thing to a human.
"""

from datetime import date, timedelta
from decimal import Decimal

from screener.scoring import CODES, compute, months_before


def _series(end: date, days: int, close: str = "100") -> list[tuple[date, Decimal]]:
    """`days` consecutive calendar days ending on `end`, all at one close."""
    return [(end - timedelta(days=days - 1 - i), Decimal(close)) for i in range(days)]


def test_the_codes_are_the_four_seeded_metric_codes():
    assert CODES == ("ret_3m", "ret_6m", "ret_12m", "off_52w_high")


def test_months_before_walks_calendar_months():
    assert months_before(date(2026, 9, 6), 3) == date(2026, 6, 6)
    assert months_before(date(2026, 9, 6), 12) == date(2025, 9, 6)


def test_months_before_clamps_onto_a_shorter_month():
    # 31 May minus three months is 28 February, not an invalid 31 February.
    assert months_before(date(2026, 5, 31), 3) == date(2026, 2, 28)


def test_a_flat_series_returns_zero_on_every_window():
    as_of = date(2026, 9, 6)
    values = compute(_series(as_of, 500), as_of)

    assert values["ret_3m"] == Decimal(0)
    assert values["ret_6m"] == Decimal(0)
    assert values["ret_12m"] == Decimal(0)
    assert values["off_52w_high"] == Decimal(0)


def test_a_doubling_over_the_year_is_a_one_hundred_percent_twelve_month_return():
    as_of = date(2026, 9, 6)
    old = [(as_of - timedelta(days=400 - i), Decimal(100)) for i in range(340)]
    new = [(as_of - timedelta(days=60 - i), Decimal(200)) for i in range(61)]

    values = compute(old + new, as_of)

    assert values["ret_12m"] == Decimal(1)


def test_off_52w_high_is_negative_below_the_high_and_never_positive():
    as_of = date(2026, 9, 6)
    series = _series(as_of, 500, "100")
    peak = as_of - timedelta(days=30)
    series = [(day, Decimal(200) if day == peak else close) for day, close in series]

    values = compute(series, as_of)

    assert values["off_52w_high"] == Decimal("-0.5")


def test_a_window_the_history_does_not_cover_is_absent_rather_than_wrong():
    # A security with five bars computes nothing at all (spec D8).
    as_of = date(2026, 9, 6)

    assert compute(_series(as_of, 5), as_of) == {}


def test_a_short_history_contributes_the_windows_it_does_cover():
    as_of = date(2026, 9, 6)

    values = compute(_series(as_of, 200), as_of)

    assert set(values) == {"ret_3m", "ret_6m"}


def test_the_nearest_bar_at_or_before_the_target_is_used():
    # No bar exactly three months back; the one before it answers, so a
    # holiday needs no market calendar.
    as_of = date(2026, 9, 6)
    series = [
        (date(2025, 1, 1), Decimal(50)),
        (date(2026, 6, 1), Decimal(100)),
        (as_of, Decimal(150)),
    ]

    values = compute(series, as_of)

    assert values["ret_3m"] == Decimal("0.5")


def test_bars_after_as_of_are_not_read():
    as_of = date(2026, 9, 6)
    series = _series(as_of, 500) + [(as_of + timedelta(days=1), Decimal(999))]

    assert compute(series, as_of)["off_52w_high"] == Decimal(0)


def test_an_empty_series_computes_nothing():
    assert compute([], date(2026, 9, 6)) == {}
