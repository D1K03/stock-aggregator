"""The snapshot row: a weighted mean, and three columns saying what it is worth.

`blended_score` is a materialised derivation, not ground truth. The three
columns beside it are what stop it being read as one.
"""

from decimal import Decimal

from screener.scoring import PillarScore, blend


def _pillar(score: str, coverage: str = "1") -> PillarScore:
    return PillarScore(Decimal(score), 4, Decimal(coverage))


def test_one_pillar_at_full_weight_blends_to_exactly_that_pillar_score():
    got = blend({"momentum": _pillar("82")}, {"momentum": Decimal(1)}, [1])

    assert got is not None
    assert got.blended_score == Decimal(82)


def test_weights_are_normalised_over_the_pillars_actually_present():
    # Weights are stored raw and normalised at read time, so a version whose
    # weights sum to 3 cannot produce a score three times too small.
    got = blend(
        {"momentum": _pillar("80"), "quality": _pillar("40")},
        {"momentum": Decimal(2), "quality": Decimal(1)},
        [1, 1],
    )

    assert got is not None
    assert got.blended_score == Decimal(200) / 3


def test_a_pillar_with_no_weight_in_this_version_does_not_reach_the_blend():
    got = blend(
        {"momentum": _pillar("80"), "sentiment": _pillar("10")},
        {"momentum": Decimal(1)},
        [1, 1],
    )

    assert got is not None
    assert got.blended_score == Decimal(80)


def test_pillar_agreement_counts_pillars_at_or_above_the_seventy_fifth_percentile():
    got = blend(
        {"momentum": _pillar("75"), "quality": _pillar("74")},
        {"momentum": Decimal(1), "quality": Decimal(1)},
        [1, 1],
    )

    assert got is not None
    assert got.pillar_agreement == 1


def test_min_coverage_is_the_worst_of_the_contributing_pillars():
    got = blend(
        {"momentum": _pillar("80", "0.5"), "quality": _pillar("80", "1")},
        {"momentum": Decimal(1), "quality": Decimal(1)},
        [1, 1],
    )

    assert got is not None
    assert got.min_coverage == Decimal("0.5")


def test_worst_fallback_level_takes_the_minimum_because_higher_is_more_specific():
    # 2 industry -> 1 sector -> 0 market.
    got = blend({"momentum": _pillar("80")}, {"momentum": Decimal(1)}, [1, 0, 1])

    assert got is not None
    assert got.worst_fallback_level == 0


def test_no_pillars_is_no_snapshot():
    assert blend({}, {"momentum": Decimal(1)}, []) is None


def test_no_weight_on_any_present_pillar_is_no_snapshot():
    assert blend({"sentiment": _pillar("10")}, {"momentum": Decimal(1)}, [1]) is None
