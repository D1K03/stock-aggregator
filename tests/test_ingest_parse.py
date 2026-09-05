import json
from datetime import date
from decimal import Decimal

from screener.ingest.parse import Action, Bar, parse


def chart(timestamps, quote, events=None):
    body = {
        "chart": {
            "result": [
                {
                    "meta": {"currency": "USD", "symbol": "AAPL"},
                    "timestamp": timestamps,
                    "indicators": {"quote": [quote]},
                }
            ],
            "error": None,
        }
    }
    if events is not None:
        body["chart"]["result"][0]["events"] = events
    return json.dumps(body).encode()


def test_bars_come_back_as_decimals_and_a_date():
    payload = chart(
        [1758585600],  # 2025-09-23T00:00:00Z
        {
            "open": [100.5],
            "high": [101.0],
            "low": [99.5],
            "close": [100.0],
            "volume": [1234567],
        },
    )
    bars, _ = parse(payload)
    assert bars == [
        Bar(
            trade_date=date(2025, 9, 23),
            open=Decimal("100.5"),
            high=Decimal("101.0"),
            low=Decimal("99.5"),
            close=Decimal("100.0"),
            volume=1234567,
        )
    ]


def test_a_bar_with_a_null_field_is_dropped_not_zero_filled():
    # Yahoo pads its quote arrays with nulls. price_daily's columns are not
    # null, and a zero-filled bar is a fabricated -100% return.
    payload = chart(
        [1758585600, 1758672000],
        {
            "open": [100.0, None],
            "high": [101.0, None],
            "low": [99.0, None],
            "close": [100.0, None],
            "volume": [10, None],
        },
    )
    bars, _ = parse(payload)
    assert len(bars) == 1
    assert bars[0].trade_date == date(2025, 9, 23)


def test_splits_and_dividends_are_both_read():
    payload = chart(
        [1758585600],
        {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1]},
        events={
            "splits": {
                "1758585600": {
                    "date": 1758585600,
                    "numerator": 2,
                    "denominator": 1,
                    "splitRatio": "2:1",
                }
            },
            "dividends": {"1758585600": {"date": 1758585600, "amount": 0.24}},
        },
    )
    _, actions = parse(payload)
    assert Action(date(2025, 9, 23), "split", Decimal("2"), None) in actions
    assert Action(date(2025, 9, 23), "dividend", None, Decimal("0.24")) in actions


def test_no_events_block_means_no_actions_not_an_error():
    payload = chart(
        [1758585600],
        {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1]},
    )
    bars, actions = parse(payload)
    assert len(bars) == 1
    assert actions == []


def test_an_error_response_yields_nothing_rather_than_raising():
    payload = json.dumps({"chart": {"result": None, "error": {"code": "Not Found"}}}).encode()
    assert parse(payload) == ([], [])


def test_malformed_json_yields_nothing_rather_than_raising():
    assert parse(b"<html>bad gateway</html>") == ([], [])


def test_bars_come_back_in_date_order():
    payload = chart(
        [1758672000, 1758585600],
        {
            "open": [2.0, 1.0],
            "high": [2.0, 1.0],
            "low": [2.0, 1.0],
            "close": [2.0, 1.0],
            "volume": [2, 1],
        },
    )
    bars, _ = parse(payload)
    assert [b.trade_date for b in bars] == sorted(b.trade_date for b in bars)
