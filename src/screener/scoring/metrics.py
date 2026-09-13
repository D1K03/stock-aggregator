"""The four momentum metrics, from one security's adjusted series.

All four are higher-is-better and all four are calendar-windowed. A window the
history does not cover produces no value at all rather than a value computed
from less history than it claims (spec D8): a metric that is missing lowers
coverage visibly, while one quietly computed off five bars does not.
"""

import calendar
from bisect import bisect_right
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal

from screener.scoring.basis import Absent

# The same four `metric.code` values migration 019 seeds. They agree with that
# file by hand, which is the whole reason the seed is a migration and not a
# command.
CODES: tuple[str, ...] = ("ret_3m", "ret_6m", "ret_12m", "off_52w_high")

_RETURN_MONTHS: dict[str, int] = {"ret_3m": 3, "ret_6m": 6, "ret_12m": 12}

# 52 weeks as days, so no month arithmetic is needed for the one window that is
# defined in weeks.
_52W_DAYS = 364


def months_before(day: date, months: int) -> date:
    """`months` calendar months before `day`, clamped onto a shorter month."""
    total = day.year * 12 + (day.month - 1) - months
    year, month = divmod(total, 12)
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _at_or_before(
    series: Sequence[tuple[date, Decimal]], target: date
) -> Decimal | None:
    """The close on the newest bar at or before `target`, or None.

    None when the series begins after `target` -- which is what makes an
    uncovered window absent rather than approximated from whatever is nearest.
    """
    index = bisect_right([day for day, _ in series], target) - 1
    return series[index][1] if index >= 0 else None


def explain_momentum(
    series: Sequence[tuple[date, Decimal]], as_of: date
) -> dict[str, Decimal | Absent]:
    """All four momentum metrics, each as its value or the reason it has none.

    The coverage rule is spec D8's: a window the history does not reach gives no
    value rather than one computed from less history than it claims.
    """
    ordered = sorted(series)
    latest = _at_or_before(ordered, as_of)
    if latest is None:
        return {code: Absent("no visible bars") for code in CODES}

    out: dict[str, Decimal | Absent] = {}
    for code, months in _RETURN_MONTHS.items():
        past = _at_or_before(ordered, months_before(as_of, months))
        if past is None:
            out[code] = Absent(f"price history shorter than the {months}-month window")
        elif past <= 0:
            out[code] = Absent("a past close ≤ 0")
        else:
            out[code] = latest / past - 1

    window_start = as_of - timedelta(days=_52W_DAYS)
    # The same coverage rule as the return windows: a series that begins inside
    # the window would otherwise report "at its 52-week high" off a fortnight.
    if ordered[0][0] > window_start:
        out["off_52w_high"] = Absent("price history shorter than the 52-week window")
    else:
        window = [close for day, close in ordered if window_start <= day <= as_of]
        # A series can start before the window and still have nothing in it:
        # read_bars reaches back 13 months, so prices that stopped about a year
        # ago land here. `max([])` used to raise and fail the whole night (plan
        # amendment P1).
        if not window:
            out["off_52w_high"] = Absent("no bars in the 52-week window")
        else:
            high = max(window)
            out["off_52w_high"] = latest / high - 1 if high > 0 else Absent("52-week high ≤ 0")
    return {code: out[code] for code in CODES}


def compute(
    series: Sequence[tuple[date, Decimal]], as_of: date
) -> dict[str, Decimal]:
    """The computable metrics for one security, keyed by `metric.code`."""
    return {
        code: value
        for code, value in explain_momentum(series, as_of).items()
        if not isinstance(value, Absent)
    }
