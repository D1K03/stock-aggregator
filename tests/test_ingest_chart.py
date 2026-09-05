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
