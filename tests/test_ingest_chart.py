from datetime import date

import httpx
import pytest

from screener.fetch import Lane, LanePool
from screener.ingest.chart import ChartClient


def responder(*responses):
    seen: list[httpx.Request] = []
    remaining = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return remaining.pop(0) if remaining else httpx.Response(200, text="{}")

    return httpx.MockTransport(handler), seen


def pool(transport, names=("one",)):
    return LanePool([Lane(name, transport=transport) for name in names])


def test_a_fetch_asks_for_the_computed_window_and_daily_bars():
    transport, seen = responder(httpx.Response(200, text='{"chart":{}}'))
    client = ChartClient(lanes=pool(transport))
    client.fetch("AAPL", date(2026, 9, 1), date(2026, 9, 5))
    url = str(seen[0].url)
    assert "/v8/finance/chart/AAPL" in url
    assert "interval=1d" in url
    assert "events=div%2Csplit" in url or "events=div,split" in url
    # period1/period2 rather than range=: the window is computed per security.
    assert "period1=" in url and "period2=" in url


def test_a_fetch_returns_the_raw_bytes_because_they_get_hashed_and_stored():
    body = '{"chart":{"result":[]}}'
    transport, _ = responder(httpx.Response(200, text=body))
    got = ChartClient(lanes=pool(transport)).fetch("AAPL", date(2026, 9, 1), date(2026, 9, 5))
    assert got == body.encode()


def test_a_404_returns_none_rather_than_raising():
    # CWEN-A 404s on both endpoints. One bad symbol must not end a night.
    transport, _ = responder(httpx.Response(404, text="not found"))
    assert ChartClient(lanes=pool(transport)).fetch("CWEN-A", date(2026, 9, 1), date(2026, 9, 5)) is None


def test_a_429_parks_the_lane_and_the_retry_leaves_by_a_different_exit():
    transport, seen = responder(
        httpx.Response(429, text="slow down"),
        httpx.Response(200, text='{"chart":{}}'),
    )
    lanes = pool(transport, names=("one", "two"))
    client = ChartClient(lanes=lanes, sleep=lambda _s: None)
    assert client.fetch("AAPL", date(2026, 9, 1), date(2026, 9, 5)) is not None
    assert len(seen) == 2


def test_a_transport_error_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    client = ChartClient(lanes=pool(httpx.MockTransport(handler)))
    assert client.fetch("AAPL", date(2026, 9, 1), date(2026, 9, 5)) is None


def test_an_unconfigured_environment_gives_one_direct_lane(monkeypatch):
    monkeypatch.delenv("BRIGHTDATA_PROXY_IPS", raising=False)
    transport, _ = responder(httpx.Response(200, text="{}"))
    with ChartClient(transport=transport) as client:
        assert len(client.lanes) == 1


def test_period2_is_after_the_end_date_so_todays_session_is_included():
    # `_epoch` is midnight UTC and a US daily bar carries its session-open
    # timestamp (13:30/14:30 UTC), so `period2 = end 00:00Z` always excludes the
    # session that closed today. Coverage must not depend on the hour cron fires.
    from datetime import datetime, time as clock, timezone
    from urllib.parse import parse_qs, urlparse

    transport, seen = responder(httpx.Response(200, text='{"chart":{}}'))
    end = date(2026, 9, 5)
    ChartClient(lanes=pool(transport)).fetch("AAPL", date(2026, 9, 1), end)

    period2 = int(parse_qs(urlparse(str(seen[0].url)).query)["period2"][0])
    end_midnight = int(
        datetime.combine(end, clock.min, tzinfo=timezone.utc).timestamp()
    )
    assert period2 > end_midnight


def test_a_symbol_is_escaped_into_the_url_path():
    # An unescaped symbol is the concrete route to `httpx.InvalidURL`, which is
    # not an `httpx.HTTPError` and so would escape `_request` and end the night.
    transport, seen = responder(httpx.Response(200, text='{"chart":{}}'))
    client = ChartClient(lanes=pool(transport))
    client.fetch("A B/C", date(2026, 9, 1), date(2026, 9, 5))
    assert "A%20B%2FC" in str(seen[0].url)
    # A perfectly ordinary symbol is left alone.
    client.fetch("BRK-B", date(2026, 9, 1), date(2026, 9, 5))
    assert str(seen[1].url).count("BRK-B") == 1
