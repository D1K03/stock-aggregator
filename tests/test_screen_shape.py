"""Typed rows into the screen's JSON (ui-swap spec D5, D9, D11)."""

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from screener.scoring import CODES, RATIO_CODES
from screener.screen import (
    UNITS,
    ActionRow,
    BarRow,
    Check,
    ClassificationRow,
    MetricInfoRow,
    MetricRow,
    PillarRow,
    PreviousRunRow,
    Reproduction,
    RunRow,
    ScreenRow,
    SectorRow,
    SnapshotRow,
    SymbolRow,
    TilesRow,
    closes,
    delta,
    exact,
    one_decimal,
    price,
    screen_payload,
    screen_row,
    security_payload,
)

GEF = ScreenRow(
    11, "GEF", "Greif", "consumer-cyclical", "Consumer Cyclical", Decimal("84.8"), None,
    None, None, Decimal("79"), Decimal("0.75"), Decimal("91"), Decimal("1"), 2, Decimal("0"),
)
RUN = RunRow(
    7, date(2026, 9, 13), datetime(2026, 9, 13, 23, tzinfo=timezone.utc),
    datetime(2026, 9, 13, 23, 19, tzinfo=timezone.utc), "6b1c111", "9f3a71c2", "v2", 3,
    108000, "v2 momentum", False,
)


def _at(day: int) -> datetime:
    return datetime(2026, 3, day, 23, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "value,shown",
    [(Decimal("84.85"), "84.9"), (Decimal("84.8499"), "84.8"), (Decimal("79"), "79.0"), (None, None)],
)
def test_scores_have_one_decimal_rounded_half_up(value, shown):
    assert one_decimal(value) == shown


@pytest.mark.parametrize(
    "value,shown",
    [(Decimal("0.0741"), "0.0741"), (Decimal("1E+1"), "10"), (Decimal("0.75"), "0.75"), (None, None)],
)
def test_raw_values_are_exact_and_never_in_exponent_form(value, shown):
    assert exact(value) == shown


@pytest.mark.parametrize(
    "value,shown",
    [
        (Decimal("41.2"), "41.20"),
        (Decimal("999.994"), "999.99"),
        (Decimal("1000.4"), "1000"),
        (Decimal("2718.5"), "2719"),
    ],
)
def test_closes_have_two_decimals_below_a_thousand_and_none_from_it(value, shown):
    assert price(value) == shown


def test_three_metrics_are_multiples_and_every_other_is_a_percent():
    assert set(UNITS) == {*RATIO_CODES, *CODES}
    assert {code for code, unit in UNITS.items() if unit == "multiple"} == {
        "book_yield", "debt_to_equity", "interest_cover",
    }


def test_delta_is_null_without_a_previous_score():
    assert delta(GEF) is None
    assert delta(replace(GEF, previous_score=Decimal("80.1"))) == Decimal("4.7")


def test_a_split_fetched_before_its_bar_is_applied_and_one_fetched_after_is_not():
    split = [ActionRow(4, date(2026, 3, 3), "split", Decimal(2), None)]
    # Fetched the night before the split: Yahoo had not restated it yet.
    unrestated = [
        BarRow(4, date(2026, 3, 2), Decimal("200"), _at(2)),
        BarRow(4, date(2026, 3, 3), Decimal("100"), _at(3)),
    ]
    # Refetched after the split: Yahoo already halved it, so halving again would
    # be the double count PR #49 fixed.
    restated = [
        BarRow(4, date(2026, 3, 2), Decimal("100"), _at(4)),
        BarRow(4, date(2026, 3, 3), Decimal("100"), _at(4)),
    ]

    continuous = [["2026-03-02", "100.00"], ["2026-03-03", "100.00"]]
    assert closes(unrestated, split, 60) == continuous
    assert closes(restated, split, 60) == continuous


def test_only_the_newest_closes_are_kept():
    bars = [BarRow(4, date(2026, 3, day), Decimal(day), _at(day)) for day in range(1, 6)]

    assert closes(bars, [], 2) == [["2026-03-04", "4.00"], ["2026-03-05", "5.00"]]


def test_a_partial_row_with_an_absent_pillar():
    assert screen_row(GEF, [["2026-03-02", "41.20"]]) == {
        "symbol": "GEF",
        "name": "Greif",
        "sector": {"code": "consumer-cyclical", "name": "Consumer Cyclical"},
        "score": "84.8",
        "delta": None,
        "pillars": {
            "V": None,
            "Q": {"score": "79.0", "coverage": "0.75"},
            "M": {"score": "91.0", "coverage": "1"},
        },
        "agreement": 2,
        "min_coverage": "0",
        "partial": True,
        "closes": [["2026-03-02", "41.20"]],
    }


def test_a_fully_covered_row_is_not_partial():
    assert screen_row(replace(GEF, min_coverage=Decimal(1)), [])["partial"] is False


def test_the_page_carries_its_run_its_previous_night_and_its_tiles():
    shown = screen_payload(
        run=RUN,
        latest=True,
        previous=PreviousRunRow(6, date(2026, 9, 12)),
        tiles=TilesRow(1499, 1504, 41, 58, 15),
        sectors=[SectorRow("technology", "Technology")],
        total=1,
        rows=[GEF],
        closes_by_security={},
    )

    assert set(shown) == {
        "state", "latest", "run", "previous_as_of", "tiles", "sectors", "total", "rows",
    }
    assert shown["state"] == "ready"
    assert shown["run"] == {
        "id": 7, "as_of": "2026-09-13", "started_at": "2026-09-13T23:00:00+00:00",
        "finished_at": "2026-09-13T23:19:00+00:00", "git_sha": "6b1c111",
        "config_hash": "9f3a71c2", "weight_version": "v2", "cutoff_offset_seconds": 108000,
        "logic": "v2 momentum", "emits_alerts": False,
    }
    assert shown["previous_as_of"] == "2026-09-12"
    assert shown["tiles"] == {
        "scored": 1499, "active_now": 1504, "partial": 41, "agreement_3": 58,
        "market_ranked_values": 15,
    }
    assert shown["sectors"] == [{"code": "technology", "name": "Technology"}]
    assert shown["rows"][0]["closes"] == []


def test_present_only_counts_stored_metrics_still_applicable_after_a_reclassification():
    # Stored under STANDARD (earnings_yield, ebitda_ev, fcf_yield); reclassified
    # since to a class expecting only earnings_yield and book_yield (fix round 1).
    expected = {"valuation": ("earnings_yield", "book_yield"), "quality": (), "momentum": ()}
    stored_codes = ("earnings_yield", "ebitda_ev", "fcf_yield")
    metrics = [
        MetricRow(code, Decimal("0.1"), Decimal("50"), "Technology", 30, 1, "TTM", date(2026, 3, 2))
        for code in stored_codes
    ]
    shown_codes = ("earnings_yield", "book_yield", "ebitda_ev", "fcf_yield")
    # `listed` walks every code scoring knows, so `info` needs a pillar for each
    # even though only the valuation four end up shown for this security.
    valuation_codes = {"earnings_yield", "ebitda_ev", "fcf_yield", "book_yield", "ffo_yield"}
    info = {
        code: MetricInfoRow(
            code, code, True, "valuation" if code in valuation_codes else "quality"
        )
        for code in RATIO_CODES
    } | {code: MetricInfoRow(code, code, True, "momentum") for code in CODES}
    checks = {code: Check("ok", Decimal("0.1"), None) for code in shown_codes}

    shown = security_payload(
        run=RUN,
        latest=True,
        match=SymbolRow(11, "GEF", "Greif", "XNYS", True),
        where=ClassificationRow("industrials", "Industrials", "packaging", "Packaging"),
        snapshot=SnapshotRow(Decimal("70"), 1, Decimal("1")),
        pillars=[PillarRow("valuation", Decimal("70"), 1, Decimal("1"))],
        metrics=metrics,
        info=info,
        expected=expected,
        checks=checks,
        reproduction=Reproduction(datetime(2026, 3, 3, 6, tzinfo=timezone.utc), {}, frozenset()),
        running_build="6b1c111",
        closes=[],
    )

    valuation = next(p for p in shown["pillars"] if p["code"] == "valuation")
    assert (valuation["present"], valuation["expected"]) == (1, 2)
    assert {m["code"] for m in valuation["metrics"]} == set(shown_codes)
