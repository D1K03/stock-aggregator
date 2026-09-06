"""A pillar is the mean of the metric percentiles it actually has.

A missing metric is dropped, never imputed, and the resulting coverage is what
records that it was missing.
"""

from decimal import Decimal

from screener.scoring import score_pillar


def test_full_coverage_averages_every_metric():
    got = score_pillar(
        {"a": Decimal(10), "b": Decimal(20), "c": Decimal(30), "d": Decimal(40)},
        expected=4,
    )

    assert got is not None
    assert got.score == Decimal(25)
    assert got.metric_count == 4
    assert got.coverage == Decimal(1)


def test_a_missing_metric_lowers_coverage_without_moving_the_mean():
    full = score_pillar({"a": Decimal(10), "b": Decimal(30)}, expected=2)
    partial = score_pillar({"a": Decimal(10), "b": Decimal(30)}, expected=4)

    assert full is not None and partial is not None
    assert partial.score == full.score == Decimal(20)
    assert partial.metric_count == 2
    assert partial.coverage == Decimal("0.5")


def test_no_metrics_is_no_pillar_rather_than_a_zero():
    # A zero-coverage score reads as "scored badly" when the truth is "not
    # scored", and the screen has no way to tell those apart (spec D8).
    assert score_pillar({}, expected=4) is None
