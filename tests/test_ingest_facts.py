"""Timeseries JSON to facts. No I/O, no database, no clock.

The awkward cases are the point: Yahoo pads its arrays with nulls, reports a
fiscal Q4 on the same date as its fiscal year, and names its own currency.
"""

import json
from datetime import date
from decimal import Decimal

from screener.ingest import SERIES, Fact, parse_facts as parse


def _payload(*series: dict) -> bytes:
    return json.dumps({"timeseries": {"result": list(series)}}).encode()


def _entry(as_of: str, raw, currency: str | None = "USD") -> dict:
    entry = {"asOfDate": as_of, "reportedValue": {"raw": raw}}
    if currency is not None:
        entry["currencyCode"] = currency
    return entry


def test_the_series_map_matches_the_seeded_metric_codes():
    assert len(SERIES) == 28
    assert SERIES["TotalRevenue"] == "revenue"
    assert SERIES["OrdinarySharesNumber"] == "shares_outstanding"
    assert SERIES["BasicAverageShares"] == "shares_basic_avg"


def test_no_derivation_is_in_the_series_map():
    # D3: EBITDA and free cash flow were measured as exactly reproducible.
    assert "EBITDA" not in SERIES
    assert "FreeCashFlow" not in SERIES


def test_an_annual_series_parses_to_facts():
    payload = _payload({
        "meta": {"symbol": ["AAPL"], "type": ["annualTotalRevenue"]},
        "annualTotalRevenue": [_entry("2025-09-30", 416161000000)],
    })

    facts = parse(payload)

    assert facts == [
        Fact("revenue", date(2025, 9, 30), "A",
             Decimal("416161000000"), "USD")
    ]


def test_the_quarterly_prefix_becomes_period_type_q():
    payload = _payload({
        "meta": {"symbol": ["AAPL"], "type": ["quarterlyTotalRevenue"]},
        "quarterlyTotalRevenue": [_entry("2026-06-30", 109417000000)],
    })

    assert parse(payload)[0].period_type == "Q"


def test_a_quarter_and_a_year_sharing_a_period_end_both_survive():
    # F5: a fiscal Q4 ends when its fiscal year does. Both facts must exist,
    # distinctly, or the collapse D6 describes starts here in the parser.
    payload = _payload(
        {
            "meta": {"type": ["annualTotalRevenue"]},
            "annualTotalRevenue": [_entry("2025-09-30", 416161000000)],
        },
        {
            "meta": {"type": ["quarterlyTotalRevenue"]},
            "quarterlyTotalRevenue": [_entry("2025-09-30", 102466000000)],
        },
    )

    facts = parse(payload)

    assert len(facts) == 2
    assert {f.period_type for f in facts} == {"A", "Q"}
    assert len({f.value for f in facts}) == 2


def test_nulls_in_the_array_are_skipped_not_stored_as_zero():
    # A null becoming 0 would be a fabricated fundamental.
    payload = _payload({
        "meta": {"type": ["annualNetIncome"]},
        "annualNetIncome": [None, _entry("2025-09-30", 112010000000), None],
    })

    facts = parse(payload)

    assert len(facts) == 1
    assert facts[0].value == Decimal("112010000000")


def test_an_entry_with_no_reported_value_is_skipped():
    payload = _payload({
        "meta": {"type": ["annualNetIncome"]},
        "annualNetIncome": [{"asOfDate": "2025-09-30"}],
    })

    assert parse(payload) == []


def test_the_currency_comes_from_the_payload():
    payload = _payload({
        "meta": {"type": ["annualTotalRevenue"]},
        "annualTotalRevenue": [_entry("2025-09-30", 1, currency="GBP")],
    })

    assert parse(payload)[0].currency == "GBP"


def test_a_value_without_a_currency_stores_none_rather_than_usd():
    # A null means "the provider did not say", which is true. Assuming dollars
    # might not be, and the assumption would be unrecoverable.
    payload = _payload({
        "meta": {"type": ["annualTotalRevenue"]},
        "annualTotalRevenue": [_entry("2025-09-30", 1, currency=None)],
    })

    assert parse(payload)[0].currency is None


def test_a_series_we_do_not_want_is_ignored():
    payload = _payload({
        "meta": {"type": ["annualEBITDA"]},
        "annualEBITDA": [_entry("2025-09-30", 144748000000)],
    })

    assert parse(payload) == []


def test_a_negative_value_is_kept_as_reported():
    # Capital expenditure arrives negative. Sign conventions are the consuming
    # ratio's problem, not the fact's.
    payload = _payload({
        "meta": {"type": ["annualCapitalExpenditure"]},
        "annualCapitalExpenditure": [_entry("2025-09-30", -12715000000)],
    })

    assert parse(payload)[0].value == Decimal("-12715000000")


def test_an_empty_or_error_payload_parses_to_nothing():
    assert parse(json.dumps({"timeseries": {"result": []}}).encode()) == []
    assert parse(json.dumps({"timeseries": {"error": "nope"}}).encode()) == []


def test_a_non_finite_value_is_refused_rather_than_stored():
    # Python's json decoder accepts bare NaN; `numeric` would take it and every
    # later comparison against it would be false.
    payload = json.dumps(
        {"timeseries": {"result": [{
            "meta": {"type": ["annualTotalRevenue"]},
            "annualTotalRevenue": [
                {"asOfDate": "2025-09-30",
                 "reportedValue": {"raw": float("nan")},
                 "currencyCode": "USD"},
            ],
        }]}}
    ).encode()

    assert parse(payload) == []
