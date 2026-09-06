"""Raw closes plus corporate actions into an adjusted total-return series.

Anchored at the present: the newest bar needs no adjustment, so the last price
in the series is the price a reader recognises. Every earlier bar carries the
product of the factors of every action that has happened since.

    adjusted_close(t) = raw_close(t) x  factors for every action with
                                        effective_date > t

        split, ratio r        factor = 1 / r
        dividend, amount D    factor = (P - D) / P,  P = the close on the bar
                                       before the ex-date

Total return rather than price return (spec D3): the split half has to be built
correctly regardless, so dividends ride the same machinery rather than leaving
`corporate_action`'s dividend rows without a consumer.
"""

from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
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
    bars: Sequence[tuple[date, Decimal]],
    actions: Sequence[Action],
) -> list[tuple[date, Decimal]]:
    """`(date, adjusted_close)` ascending, from raw closes and actions."""
    ordered = sorted(bars)
    if not ordered:
        return []

    dates = [day for day, _ in ordered]
    factors: list[tuple[date, Decimal]] = []
    for action in sorted(actions, key=lambda a: a.effective_date):
        # The bar strictly before the ex-date. A dividend's factor is measured
        # against the last close that still included it.
        index = bisect_left(dates, action.effective_date) - 1
        prior = ordered[index][1] if index >= 0 else None
        factor = _factor(action, prior)
        if factor is not None:
            factors.append((action.effective_date, factor))

    # Backwards through the bars, folding in each action as we pass its date:
    # the running product is exactly "every action still ahead of this bar".
    out: list[tuple[date, Decimal]] = []
    cumulative = Decimal(1)
    remaining = len(factors) - 1
    for day, close in reversed(ordered):
        while remaining >= 0 and factors[remaining][0] > day:
            cumulative *= factors[remaining][1]
            remaining -= 1
        out.append((day, close * cumulative))
    out.reverse()
    return out
