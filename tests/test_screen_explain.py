"""Each metric's one status, from what was stored and what reproduces (ui-swap D13, D14)."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from screener.scoring import Absent
from screener.screen import DEPENDS, PRICE, Check, Reproduction, RunRow, check, view_offset

AS_OF = date(2026, 3, 2)
RUN = RunRow(
    7, AS_OF, datetime(2026, 3, 2, 23, tzinfo=timezone.utc), None, "abc1234", "9f3a", "v2", 3,
    108000, "v2 momentum", False,
)


def _reproduced(**values: Decimal | Absent) -> Reproduction:
    return Reproduction(RUN.started_at, values, frozenset())


@pytest.mark.parametrize(
    "stored,reproduced,status",
    [
        (Decimal("0.0741"), Decimal("0.0741"), "ok"),
        (Decimal("0.0741"), Decimal("0.07410"), "ok"),
        (Decimal("0.0741"), Decimal("0.0742"), "mismatch"),
        (Decimal("0.0741"), Absent("equity ≤ 0"), "mismatch"),
        (None, Absent("equity ≤ 0"), "absent"),
        (None, Decimal("0.0741"), "unexpected"),
    ],
)
def test_a_metric_gets_one_status_from_what_was_stored_and_what_reproduces(stored, reproduced, status):
    got = check(
        pillar="valuation", code="earnings_yield", stored=stored,
        reproduction=_reproduced(earnings_yield=reproduced),
    )

    assert got.status == status


def test_an_absence_carries_its_reason_and_a_value_carries_itself():
    assert check(
        pillar="quality", code="roic", stored=None,
        reproduction=_reproduced(roic=Absent("invested capital ≤ 0")),
    ) == Check("absent", None, "invested capital ≤ 0")
    assert check(
        pillar="quality", code="roic", stored=Decimal("1"),
        reproduction=_reproduced(roic=Decimal("2")),
    ) == Check("mismatch", Decimal("2"), None)


def test_a_stored_metric_that_no_longer_applies_is_a_mismatch_that_says_so():
    got = check(pillar="valuation", code="book_yield", stored=Decimal("0.5"), reproduction=_reproduced())

    assert got.status == "mismatch"
    assert got.reason is not None and "no longer applicable" in got.reason


@pytest.mark.parametrize(
    "pillar,status",
    [("momentum", "refreshed"), ("valuation", "refreshed"), ("quality", "ok")],
)
def test_refreshed_prices_skip_only_the_metrics_that_read_a_close(pillar, status):
    reproduction = Reproduction(RUN.started_at, {"m": Decimal(1)}, frozenset({"price"}))

    assert check(pillar=pillar, code="m", stored=Decimal(1), reproduction=reproduction).status == status


def test_only_a_price_input_can_be_refreshed():
    # Facts are appended, never rewritten, so a fact observed after the run is
    # outside its view rather than a change to what it saw (§13).
    assert frozenset().union(*DEPENDS.values()) == {PRICE}


def test_a_reproduction_that_could_not_run_leaves_every_metric_unchecked():
    failed = Reproduction(RUN.started_at, None, frozenset({"price"}))

    assert check(pillar="quality", code="roic", stored=Decimal(1), reproduction=failed) == Check(
        "unchecked", None, None
    )


def test_the_view_ends_at_the_run_s_start_when_that_precedes_its_cutoff():
    assert view_offset(RUN) == timedelta(hours=23)
    started_late = replace(RUN, started_at=datetime(2026, 3, 9, tzinfo=timezone.utc))
    assert view_offset(started_late) == timedelta(hours=30)
