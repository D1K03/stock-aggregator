"""Pillar scores and a weight version into one snapshot row.

`blended_score` is stored so the nightly crossing diff need not aggregate
pillar rows -- it is a derivation, and the three columns beside it are what keep
it from being read as ground truth. Under a one-pillar weight version the
blended score *is* the momentum score, and `pillar_agreement` and `min_coverage`
say so on every row.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from screener.scoring.pillars import PillarScore

# "Top quartile", per DESIGN.md. Arbitrary beyond that, and recorded in the
# spec as an open parameter.
AGREEMENT_THRESHOLD = Decimal(75)


@dataclass(frozen=True)
class Snapshot:
    blended_score: Decimal
    pillar_agreement: int
    min_coverage: Decimal
    worst_fallback_level: int


def blend(
    pillars: Mapping[str, PillarScore],
    weights: Mapping[str, Decimal],
    fallback_levels: Sequence[int],
) -> Snapshot | None:
    """One security's snapshot, or None when nothing weighted is present."""
    # Normalised over the pillars actually present, not over the version's
    # whole weight list: a pillar that produced no score must not silently
    # drag the blend towards zero by keeping its share of the denominator.
    contributing = {
        code: pillar
        for code, pillar in pillars.items()
        if weights.get(code, Decimal(0)) > 0
    }
    if not contributing:
        return None

    total_weight = sum(
        (weights[code] for code in contributing), Decimal(0)
    )
    weighted = sum(
        (weights[code] * pillar.score for code, pillar in contributing.items()),
        Decimal(0),
    )
    return Snapshot(
        blended_score=weighted / total_weight,
        pillar_agreement=sum(
            1 for p in contributing.values() if p.score >= AGREEMENT_THRESHOLD
        ),
        min_coverage=min(p.coverage for p in contributing.values()),
        # The minimum, because a higher level is more specific: 2 industry ->
        # 1 sector -> 0 market. "Worst" is the least specific group any of this
        # security's metrics had to fall back to.
        worst_fallback_level=min(fallback_levels) if fallback_levels else 0,
    )
