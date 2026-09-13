"""Stored closes plus corporate actions into an adjusted total-return series.

Anchored at the present: the newest bar needs no adjustment, so the last price
in the series is the price a reader recognises. Every earlier bar carries the
product of the factors of every action that has happened since -- **unless
Yahoo already applied it.**

    adjusted_close(t) = close(t) x  factors for every action with
                                    effective_date > t, where a split counts
                                    only if t was fetched before it took effect

        split, ratio r        factor = 1 / r
        dividend, amount D    factor = (P - D) / P,  P = the close on the bar
                                       before the ex-date

**Yahoo's close is split-adjusted as of the moment it was fetched, and never
dividend-adjusted** (its `adjclose` is the one that includes dividends). A stored
payload shows it: APH's 2020-01-02 close arrives as 13.65, about $108 divided by
its three 2:1 splits. So a split's factor belongs only on bars fetched *before*
the split's effective date. Applied to every earlier bar, as the scoring spec
first specified, it adjusted a second time -- a 2:1 split halved the history
again, and APH's real +9% three-month return was stored as +118.6%.

`observed_at` is what makes this answerable per bar: the settling-window upsert
rewrites a bar's close and its `observed_at` together, so a bar refetched after a
split carries both the restated close and the time that says so.

Total return rather than price return (spec D3): the split half has to be built
correctly regardless, so dividends ride the same machinery rather than leaving
`corporate_action`'s dividend rows without a consumer.
"""

from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal


@dataclass(frozen=True)
class Action:
    """One corporate action, as `corporate_action` stores it."""

    effective_date: date
    action_type: str
    ratio: Decimal | None = None
    amount: Decimal | None = None


def _factor(action: Action, prior_close: Decimal | None) -> Decimal | None:
    """The multiplier this action applies to every bar before it, or None.

    None means "not adjustable", and the caller drops it. Three things reach
    that branch and all three are better dropped than guessed:

    - a spinoff, where neither `ratio` nor `amount` carries the same meaning,
      and inventing a factor would be an invisible fiction inside a number
      whose whole purpose is to be traceable;
    - a dividend whose ex-date precedes every bar we hold, so there is no P;
    - a dividend at or above the prior close, where (P - D) / P is zero or
      negative and would flatten or invert the entire history behind it.
    """
    if action.action_type == "split":
        if action.ratio is None or action.ratio <= 0:
            return None
        return Decimal(1) / action.ratio
    if action.action_type == "dividend":
        if action.amount is None or prior_close is None:
            return None
        if action.amount <= 0 or action.amount >= prior_close:
            return None
        return (prior_close - action.amount) / prior_close
    return None


def adjusted_closes(
    bars: Sequence[tuple[date, Decimal, datetime]],
    actions: Sequence[Action],
) -> list[tuple[date, Decimal]]:
    """`(date, adjusted_close)` ascending, from `(date, close, observed_at)` bars."""
    ordered = sorted(bars, key=lambda bar: bar[0])
    if not ordered:
        return []

    dates = [day for day, _, _ in ordered]
    dividends: list[tuple[date, Decimal]] = []
    splits: list[tuple[date, Decimal]] = []
    for action in sorted(actions, key=lambda a: a.effective_date):
        # The bar strictly before the ex-date. A dividend's factor is measured
        # against the last close that still included it.
        index = bisect_left(dates, action.effective_date) - 1
        prior = ordered[index][1] if index >= 0 else None
        factor = _factor(action, prior)
        if factor is None:
            continue
        if action.action_type == "split":
            splits.append((action.effective_date, factor))
        else:
            dividends.append((action.effective_date, factor))

    # Backwards through the bars, folding in each dividend as we pass its date:
    # the running product is exactly "every dividend still ahead of this bar".
    # Splits cannot share that product, because whether one applies depends on
    # when each bar was fetched rather than only on its date.
    out: list[tuple[date, Decimal]] = []
    cumulative = Decimal(1)
    remaining = len(dividends) - 1
    for day, close, observed_at in reversed(ordered):
        while remaining >= 0 and dividends[remaining][0] > day:
            cumulative *= dividends[remaining][1]
            remaining -= 1
        factor = cumulative
        for effective, split in splits:
            if effective > day and observed_at < _start_of(effective):
                factor *= split
        out.append((day, close * factor))
    out.reverse()
    return out


def _start_of(day: date) -> datetime:
    # A bar fetched at any point on the effective date counts as already
    # restated: the nightly fetch runs after that day's close, and a bar Yahoo
    # had not yet restated sits inside the settling window and is refetched.
    return datetime.combine(day, time.min, tzinfo=timezone.utc)
