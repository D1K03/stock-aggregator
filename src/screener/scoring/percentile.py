"""Where a value sits among its peers, and the group's decile cache.

    percentile = (count strictly below + (count equal - 1) / 2) / (n - 1) x 100

Spec D6 writes the divisor as `n` and the numerator's tie term over the full
equal count. Taken literally that satisfies the spec's first stated property --
an all-equal group scores 50 -- and fails its second, since the minimum of a
distinct three-member group comes out at 16.7 rather than 0. This form
satisfies both. Plan amendment A3, and the spec carries an erratum saying so.

Both readings are legitimate statistics. This one is chosen because it is the
one the spec's own tests describe.
"""

from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from decimal import Decimal

_HUNDRED = Decimal(100)


def percentiles(
    values: Sequence[Decimal], *, higher_is_better: bool = True
) -> list[Decimal]:
    """One percentile per value, in the order the values were given."""
    n = len(values)
    if n == 0:
        return []
    # A group of one has no spread to place anything within, and 50 is the only
    # answer that does not claim a rank it cannot know.
    if n == 1:
        return [Decimal(50)]

    ordered = sorted(values)
    out: list[Decimal] = []
    for value in values:
        below = bisect_left(ordered, value)
        equal = bisect_right(ordered, value) - below
        rank = Decimal(below) + (Decimal(equal) - 1) / 2
        percentile = rank / (n - 1) * _HUNDRED
        out.append(_HUNDRED - percentile if not higher_is_better else percentile)
    return out


def deciles(values: Sequence[Decimal]) -> list[Decimal]:
    """Eleven quantiles from minimum to maximum, linearly interpolated.

    A cache, not a source of truth -- the exact distribution is recoverable
    from `metric_daily` -- so interpolation between order statistics is enough
    and needs no dependency.
    """
    if not values:
        raise ValueError("deciles of an empty group")
    ordered = sorted(values)
    n = len(ordered)
    if n == 1:
        return [ordered[0]] * 11

    out: list[Decimal] = []
    for step in range(11):
        position = Decimal(step) * (n - 1) / 10
        low = int(position)
        high = min(low + 1, n - 1)
        out.append(ordered[low] + (ordered[high] - ordered[low]) * (position - low))
    return out
