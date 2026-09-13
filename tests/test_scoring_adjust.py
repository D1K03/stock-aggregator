"""Back-adjustment, anchored at the present (spec D4, amended).

The headline claim of the cycle is the first test: a company whose price did
not move across a 2-for-1 split must score a 0% return, not -50%.

The amendment is the second half of this file. Yahoo's close is already
adjusted for every split up to the moment it was fetched, so a split's factor
belongs only on bars fetched *before* the split took effect. Applied to every
earlier bar, it adjusted a second time -- APH's real +9% three-month return was
stored as +118.6%.
"""

from datetime import date, datetime, timezone
from decimal import Decimal

from screener.scoring import Action, adjusted_closes

# Before every action in this file: a bar fetched then still shows the old
# scale, which is what the tests written against raw closes describe.
EARLY = datetime(2025, 12, 1, tzinfo=timezone.utc)
# After every action in this file: Yahoo has already applied them all.
LATE = datetime(2026, 2, 1, tzinfo=timezone.utc)


def _flat(
    start_day: int, days: int, close: str, observed_at: datetime = EARLY
) -> list[tuple[date, Decimal, datetime]]:
    return [
        (date(2026, 1, start_day + i), Decimal(close), observed_at) for i in range(days)
    ]


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


# -- a split is adjusted for only where Yahoo had not already ---------------


def test_a_split_does_not_adjust_a_bar_fetched_after_it():
    # What every stored bar looks like after a backfill: Yahoo had already
    # divided the pre-split history, so the series arrives flat at 50.
    bars = _flat(1, 10, "50", observed_at=LATE)
    actions = [Action(date(2026, 1, 6), "split", ratio=Decimal(2))]

    adjusted = adjusted_closes(bars, actions)

    assert [c for _, c in adjusted] == [Decimal(50)] * 10


def test_a_split_still_adjusts_a_bar_fetched_before_it():
    # The same economics stored on the other side of the split: the pre-split
    # bars were fetched while they still read 100.
    bars = _flat(1, 5, "100", observed_at=EARLY) + _flat(6, 5, "50", observed_at=LATE)
    actions = [Action(date(2026, 1, 6), "split", ratio=Decimal(2))]

    adjusted = adjusted_closes(bars, actions)

    assert [c for _, c in adjusted] == [Decimal(50)] * 10


def test_a_series_straddling_one_split_mixes_both_regimes_correctly():
    # Days 1-3 were stored before the split and never refetched. Days 4-5 sit
    # inside the settling window, were refetched after the split already
    # adjusted, and the upsert rewrote them with a new observed_at.
    bars = (
        _flat(1, 3, "100", observed_at=EARLY)
        + _flat(4, 2, "50", observed_at=LATE)
        + _flat(6, 5, "50", observed_at=LATE)
    )
    actions = [Action(date(2026, 1, 6), "split", ratio=Decimal(2))]

    adjusted = adjusted_closes(bars, actions)

    assert [c for _, c in adjusted] == [Decimal(50)] * 10


def test_a_bar_fetched_on_the_split_date_counts_as_already_adjusted():
    # The nightly fetch runs after the close on the effective date, when Yahoo
    # has already restated. If it ever lagged, the bar sits inside the
    # settling window and is refetched adjusted on a later night.
    on_the_day = datetime(2026, 1, 6, 23, 0, tzinfo=timezone.utc)
    bars = _flat(1, 5, "50", observed_at=on_the_day)
    actions = [Action(date(2026, 1, 6), "split", ratio=Decimal(2))]

    assert [c for _, c in adjusted_closes(bars, actions)] == [Decimal(50)] * 5


def test_a_dividend_adjusts_whenever_the_bar_was_fetched():
    # Yahoo's close never includes dividends -- its adjclose does -- so the
    # dividend half of total return is always ours to apply.
    bars = _flat(1, 5, "100", observed_at=LATE) + _flat(6, 5, "100", observed_at=LATE)
    actions = [Action(date(2026, 1, 6), "dividend", amount=Decimal(1))]

    adjusted = adjusted_closes(bars, actions)

    assert adjusted[0][1] == Decimal(99)
    assert adjusted[-1][1] == Decimal(100)


def test_two_splits_adjust_each_bar_for_only_those_fetched_before():
    # A 2:1 on the 5th and a 3:1 on the 10th. The 2nd was fetched before both
    # and still reads 600; the 7th was fetched between them, after Yahoo applied
    # the first, and reads 300 -- it owes only the second split.
    between = datetime(2026, 1, 7, 23, 0, tzinfo=timezone.utc)
    bars = [
        (date(2026, 1, 2), Decimal(600), EARLY),
        (date(2026, 1, 7), Decimal(300), between),
        (date(2026, 1, 12), Decimal(100), LATE),
    ]
    actions = [
        Action(date(2026, 1, 5), "split", ratio=Decimal(2)),
        Action(date(2026, 1, 10), "split", ratio=Decimal(3)),
    ]

    adjusted = adjusted_closes(bars, actions)

    # A third has no exact decimal; ten places is far below a cent.
    assert [round(c, 10) for _, c in adjusted] == [Decimal(100)] * 3
