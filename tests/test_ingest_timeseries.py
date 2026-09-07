"""The fundamentals client: one request per security, no session.

Measured before it was written: a cold client with no cookie jar and no crumb
gets 200 from this endpoint. So this shares `LanePool` with `ChartClient` and
shares nothing at all with `screener.universe.sources.yahoo`, whose whole
cookie-and-crumb apparatus serves a different endpoint.
"""

import httpx
import pytest

from screener.ingest import TYPES, TimeseriesClient


def _transport(handler):
    return httpx.MockTransport(handler)


def test_both_prefixes_of_every_stem_are_requested():
    from screener.ingest import SERIES

    assert len(TYPES.split(",")) == len(SERIES) * 2
    assert "annualTotalRevenue" in TYPES
    assert "quarterlyTotalRevenue" in TYPES


def test_no_derivation_is_requested():
    assert "EBITDA" not in TYPES
    assert "FreeCashFlow" not in TYPES


def test_a_fetch_returns_the_body():
    def handler(request):
        assert "symbol=AAPL" in str(request.url)
        assert "type=" in str(request.url)
        return httpx.Response(200, content=b'{"timeseries":{"result":[]}}')

    with TimeseriesClient(transport=_transport(handler)) as client:
        assert client.fetch("AAPL") == b'{"timeseries":{"result":[]}}'


def test_no_crumb_is_sent():
    # The endpoint needs none, and sending one would imply a session this
    # client deliberately does not hold.
    def handler(request):
        assert "crumb" not in str(request.url)
        return httpx.Response(200, content=b"{}")

    with TimeseriesClient(transport=_transport(handler)) as client:
        client.fetch("AAPL")


def test_a_404_is_none_rather_than_an_exception():
    # One delisted symbol is one security's problem, not the night's.
    def handler(request):
        return httpx.Response(404)

    with TimeseriesClient(transport=_transport(handler)) as client:
        assert client.fetch("GONE") is None


def test_a_symbol_with_a_slash_does_not_build_an_invalid_url():
    # `InvalidURL` descends from Exception, not HTTPError, so it escapes the
    # transport guard entirely. The symbol lands in the URL path, so it is
    # escaped there as `chart.py` learned to do.
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, content=b"{}")

    with TimeseriesClient(transport=_transport(handler)) as client:
        client.fetch("BRK/B")

    assert "BRK%2FB" in seen["url"]


def test_a_transport_error_is_none_rather_than_raising():
    def handler(request):
        raise httpx.ConnectError("down")

    with TimeseriesClient(transport=_transport(handler)) as client:
        assert client.fetch("AAPL") is None


def test_a_429_parks_the_lane_and_retries():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429)
        return httpx.Response(200, content=b"{}")

    slept = []
    with TimeseriesClient(
        transport=_transport(handler), sleep=slept.append, backoff=0.01
    ) as client:
        assert client.fetch("AAPL") == b"{}"
    assert calls["n"] == 2


def test_period2_is_a_present_day_timestamp():
    # A far-future period2 makes Yahoo return 200 with series metadata but no
    # data arrays, which looks like "this security has nothing" and silently
    # loses the ingest. period2 must be the current time, injected so tests can
    # control it.
    import re
    import time

    seen = {}
    controlled_time = 1725000000  # Sept 2024

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, content=b"{}")

    with TimeseriesClient(
        transport=_transport(handler), now=lambda: float(controlled_time)
    ) as client:
        client.fetch("AAPL")

    # Extract period2 from URL
    match = re.search(r"period2=(\d+)", seen["url"])
    assert match, "period2 not found in URL"
    period2 = int(match.group(1))

    # Verify it is within a small window of our controlled time
    assert period2 == controlled_time
