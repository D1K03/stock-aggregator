"""Back-adjustment, anchored at the present (spec D4).

The headline claim of the cycle is the first test: a company whose price did
not move across a 2-for-1 split must score a 0% return, not -50%.
"""

from datetime import date
from decimal import Decimal

from screener.scoring import Action, adjusted_closes


def _flat(start_day: int, days: int, close: str) -> list[tuple[date, Decimal]]:
    return [(date(2026, 1, start_day + i), Decimal(close)) for i in range(days)]


def test_a_two_for_one_split_leaves_an_economically_flat_series_flat():
    # Raw closes step 100 -> 50 on the 6th, which is the split, not a fall.
    bars = _flat(1, 5, "100") + _flat(6, 5, "50")
    actions = [Action(date(2026, 1, 6), "split", ratio=Decimal(2))]

    adjusted = adjusted_closes(bars, actions)

    assert [c for _, c in adjusted] == [Decimal(50)] * 10
    first, last = adjusted[0][1], adjusted[-1][1]
    assert last / first - 1 == Decimal(0)


def test_without_the_action_the_same_series_reads_as_a_fifty_percent_fall():
    # The failure this exists to prevent, pinned so it cannot be mistaken for
    # a rounding difference.
    bars = _flat(1, 5, "100") + _flat(6, 5, "50")

    adjusted = adjusted_closes(bars, [])

    assert adjusted[-1][1] / adjusted[0][1] - 1 == Decimal("-0.5")


def test_a_dividend_makes_total_return_exceed_price_return_by_the_yield():
    bars = _flat(1, 5, "100") + _flat(6, 5, "100")
    actions = [Action(date(2026, 1, 6), "dividend", amount=Decimal(1))]

    adjusted = adjusted_closes(bars, actions)

    # Bars before the ex-date are scaled by (100 - 1) / 100.
    assert adjusted[0][1] == Decimal(99)
    assert adjusted[-1][1] == Decimal(100)
    assert adjusted[-1][1] / adjusted[0][1] - 1 > Decimal(0)


def test_adjustment_composes_over_two_splits_and_a_dividend():
    bars = _flat(1, 10, "100")
    actions = [
        Action(date(2026, 1, 4), "split", ratio=Decimal(2)),
        Action(date(2026, 1, 6), "dividend", amount=Decimal(1)),
        Action(date(2026, 1, 8), "split", ratio=Decimal(5)),
    ]

    adjusted = adjusted_closes(bars, actions)
    by_date = dict(adjusted)

    # The 1st precedes all three: 1/2 x 99/100 x 1/5.
    assert by_date[date(2026, 1, 1)] == Decimal(100) * (
        Decimal(1) / 2 * (Decimal(99) / 100) * (Decimal(1) / 5)
    )
    # The 7th precedes only the second split.
    assert by_date[date(2026, 1, 7)] == Decimal(100) * (Decimal(1) / 5)
    # The last bar is on or after every action, so it is untouched. That is
    # what "anchored at the present" means: the newest price is the one a
    # reader recognises.
    assert by_date[date(2026, 1, 10)] == Decimal(100)


def test_an_action_on_a_bar_date_does_not_adjust_that_bar():
    # Strictly greater than, not >=. The ex-date bar already trades ex.
    bars = _flat(1, 3, "100")
    actions = [Action(date(2026, 1, 2), "split", ratio=Decimal(2))]

    adjusted = adjusted_closes(bars, actions)

    assert [c for _, c in adjusted] == [Decimal(50), Decimal(100), Decimal(100)]


def test_a_spinoff_is_ignored_rather_than_guessed_at():
    # `corporate_action` permits 'spinoff' and neither ratio nor amount means
    # the same thing for one. Ignoring it is visible in `raw_value`; inventing
    # a factor would not be.
    bars = _flat(1, 3, "100")
    actions = [Action(date(2026, 1, 2), "spinoff", ratio=Decimal(2))]

    assert [c for _, c in adjusted_closes(bars, actions)] == [Decimal(100)] * 3


def test_a_dividend_before_the_first_bar_is_skipped_for_want_of_a_prior_close():
    bars = _flat(5, 3, "100")
    actions = [Action(date(2026, 1, 2), "dividend", amount=Decimal(1))]

    assert [c for _, c in adjusted_closes(bars, actions)] == [Decimal(100)] * 3


def test_a_dividend_at_or_above_the_prior_close_is_skipped():
    # (P - D) / P would be zero or negative and would zero the whole history.
    bars = _flat(1, 3, "10")
    actions = [Action(date(2026, 1, 2), "dividend", amount=Decimal(10))]

    assert [c for _, c in adjusted_closes(bars, actions)] == [Decimal(10)] * 3


def test_no_bars_gives_no_series():
    assert adjusted_closes([], [Action(date(2026, 1, 2), "split", ratio=Decimal(2))]) == []
