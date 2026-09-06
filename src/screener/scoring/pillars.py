"""Metric percentiles into one pillar score, with the coverage that produced it.

The mean of what is present, over the count of what was expected. Imputing a
missing metric would put an invented number inside a score whose entire claim is
that it traces back to visible raw inputs.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class PillarScore:
    score: Decimal
    metric_count: int
    coverage: Decimal


def score_pillar(
    percentiles: Mapping[str, Decimal], *, expected: int
) -> PillarScore | None:
    """One pillar's score, or None when it has no metric at all.

    None rather than a row: `pillar_score_daily.score` is not null, so a
    security with nothing to score is skipped entirely rather than given a
    meaningless number beside `coverage = 0`.
    """
    present = list(percentiles.values())
    if not present:
        return None
    return PillarScore(
        score=sum(present, Decimal(0)) / len(present),
        metric_count=len(present),
        coverage=Decimal(len(present)) / expected,
    )
