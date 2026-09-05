import json

import httpx
import pytest

from screener.fetch import Lane, LanePool
from screener.universe.sources.yahoo import (
    BROWSER,
    CrumbUnavailable,
    YahooClient,
)

PROFILE = json.dumps(
    {
        "quoteSummary": {
            "result": [
                {
                    "assetProfile": {"sector": "Technology", "industry": "Consumer Electronics"},
                    "price": {"exchange": "NMS", "currency": "USD"},
                }
            ]
        }
    }
)
UNAUTHORISED = json.dumps({"finance": {"error": {"code": "Unauthorized", "description": "Invalid Crumb"}}})
NOT_FOUND = json.dumps({"quoteSummary": {"result": None, "error": {"code": "Not Found"}}})


def scripted(script):
    """A transport driven by a list of (url_fragment, status, body) in order."""
    seen: list[httpx.Request] = []
    remaining = list(script)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        fragment, status, body = remaining.pop(0)
        assert fragment in str(request.url), f"expected {fragment} in {request.url}"
        return httpx.Response(status, text=body)

    return httpx.MockTransport(handler), seen


def test_profile_reads_sector_industry_mic_and_currency():
    transport, _ = scripted([
        ("fc.yahoo.com", 200, "ok"),
        ("getcrumb", 200, "CRUMB1"),
        ("quoteSummary/AAPL", 200, PROFILE),
    ])
    got = YahooClient(transport=transport).profile("AAPL")
    assert got is not None
    assert (got.sector, got.industry, got.mic, got.currency) == (
        "Technology", "Consumer Electronics", "XNAS", "USD",
    )


def test_the_request_asks_for_both_modules_because_assetprofile_lacks_exchange():
    transport, seen = scripted([
        ("fc.yahoo.com", 200, "ok"),
        ("getcrumb", 200, "CRUMB1"),
        ("quoteSummary/AAPL", 200, PROFILE),
    ])
    YahooClient(transport=transport).profile("AAPL")
    url = str(seen[-1].url)
    assert "assetProfile" in url and "price" in url


def test_the_crumb_is_fetched_once_and_reused_across_symbols():
    transport, _ = scripted([
        ("fc.yahoo.com", 200, "ok"),
        ("getcrumb", 200, "CRUMB1"),
        ("quoteSummary/AAPL", 200, PROFILE),
        ("quoteSummary/MSFT", 200, PROFILE),
    ])
    client = YahooClient(transport=transport)
    client.profile("AAPL")
    client.profile("MSFT")
    assert client.crumb_fetches == 1


def test_a_401_refreshes_the_crumb_and_retries_with_the_new_one():
    transport, seen = scripted([
        ("fc.yahoo.com", 200, "ok"),
        ("getcrumb", 200, "CRUMB1"),
        ("quoteSummary/AAPL", 401, UNAUTHORISED),
        ("fc.yahoo.com", 200, "ok"),
        ("getcrumb", 200, "CRUMB2"),
        ("quoteSummary/AAPL", 200, PROFILE),
    ])
    client = YahooClient(transport=transport)
    assert client.profile("AAPL") is not None
    assert client.crumb_fetches == 2
    assert "CRUMB2" in str(seen[-1].url)


def test_a_second_consecutive_401_gives_up_rather_than_looping():
    transport, _ = scripted([
        ("fc.yahoo.com", 200, "ok"),
        ("getcrumb", 200, "CRUMB1"),
        ("quoteSummary/AAPL", 401, UNAUTHORISED),
        ("fc.yahoo.com", 200, "ok"),
        ("getcrumb", 200, "CRUMB2"),
        ("quoteSummary/AAPL", 401, UNAUTHORISED),
    ])
    assert YahooClient(transport=transport).profile("AAPL") is None


def test_a_429_backs_off_and_retries():
    """The spec promises backoff on 429. Measurement says it should not fire at
    this scale, which is exactly why it needs a test rather than a live trigger."""
    transport, _ = scripted([
        ("fc.yahoo.com", 200, "ok"),
        ("getcrumb", 200, "CRUMB1"),
        ("quoteSummary/AAPL", 429, "slow down"),
        ("quoteSummary/AAPL", 200, PROFILE),
    ])
    slept: list[float] = []
    client = YahooClient(transport=transport, sleep=slept.append, backoff=0.5)
    assert client.profile("AAPL") is not None
    assert slept == [0.5]


def test_an_unknown_symbol_returns_none():
    transport, _ = scripted([
        ("fc.yahoo.com", 200, "ok"),
        ("getcrumb", 200, "CRUMB1"),
        ("quoteSummary/CWEN-A", 404, NOT_FOUND),
    ])
    assert YahooClient(transport=transport).profile("CWEN-A") is None


def test_the_cookie_from_the_handshake_travels_with_the_crumb_request():
    """The crumb is only valid alongside the cookie that was issued with it.

    A client rebuilt per request drops the jar between the handshake and the
    call it authorises, which no `MockTransport` notices and every real request
    does: Yahoo answers 401 forever. This is the test that pins the session.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "fc.yahoo.com" in str(request.url):
            response = httpx.Response(404, text="not found")
            response.headers["set-cookie"] = "A1=session-token; Path=/; Domain=.yahoo.com"
            return response
        if "getcrumb" in str(request.url):
            return httpx.Response(200, text="CRUMB1")
        return httpx.Response(200, text=PROFILE)

    client = YahooClient(transport=httpx.MockTransport(handler))
    assert client.profile("AAPL") is not None
    assert "A1=session-token" in seen[-1].headers.get("cookie", "")


def test_a_non_2xx_from_the_cookie_endpoint_is_not_fatal():
    """fc.yahoo.com answers 404 while still setting the cookie. Treating that as
    a failure would mean the handshake never completes in production."""
    def handler(request: httpx.Request) -> httpx.Response:
        if "fc.yahoo.com" in str(request.url):
            return httpx.Response(404, text="not found")
        if "getcrumb" in str(request.url):
            return httpx.Response(200, text="CRUMB1")
        return httpx.Response(200, text=PROFILE)

    assert YahooClient(transport=httpx.MockTransport(handler)).profile("AAPL") is not None


def two_lanes(script_a, script_b, now):
    """A two-lane pool, one scripted transport each, on a clock a test can wind.

    A transport per lane is the point: it is the only way to tell a crumb that
    stayed on its own lane from one that leaked onto the other.
    """
    transport_a, seen_a = scripted(script_a)
    transport_b, seen_b = scripted(script_b)
    clock = lambda: now[0]  # noqa: E731
    pool = LanePool([
        Lane("lane-1", proxy="http://p1", headers=BROWSER, transport=transport_a, clock=clock),
        Lane("lane-2", proxy="http://p2", headers=BROWSER, transport=transport_b, clock=clock),
    ])
    return pool, seen_a, seen_b


def crumbs_used(seen):
    return [
        str(r.url).split("crumb=")[1] for r in seen if "quoteSummary" in str(r.url)
    ]


def test_an_unconfigured_environment_gives_the_client_one_direct_lane(monkeypatch):
    # Yahoo starts on the proxy, so the machine with nothing configured is the
    # case that has to stay byte-for-byte what it was before lanes existed.
    for name in ("BRIGHTDATA_PROXY", "BRIGHTDATA_PROXY_IPS"):
        monkeypatch.delenv(name, raising=False)
    transport, _ = scripted([
        ("fc.yahoo.com", 200, "ok"),
        ("getcrumb", 200, "CRUMB1"),
        ("quoteSummary/AAPL", 200, PROFILE),
    ])
    client = YahooClient(transport=transport)
    assert client._lanes.names == ("direct",)
    assert client.profile("AAPL") is not None


def test_two_lanes_each_carry_their_own_crumb():
    now = [0.0]
    pool, seen_a, seen_b = two_lanes(
        [("fc.yahoo.com", 200, "ok"), ("getcrumb", 200, "CRUMB_A"),
         ("quoteSummary/AAPL", 200, PROFILE)],
        [("fc.yahoo.com", 200, "ok"), ("getcrumb", 200, "CRUMB_B"),
         ("quoteSummary/MSFT", 200, PROFILE)],
        now,
    )
    client = YahooClient(lanes=pool)
    assert client.profile("AAPL") is not None
    assert client.profile("MSFT") is not None

    # One handshake per lane, not one per symbol, and neither crumb crossed.
    assert client.crumb_fetches == 2
    assert crumbs_used(seen_a) == ["CRUMB_A"]
    assert crumbs_used(seen_b) == ["CRUMB_B"]


def test_a_429_parks_a_lane_and_the_retry_leaves_by_a_different_exit():
    now = [0.0]
    pool, seen_a, seen_b = two_lanes(
        [("fc.yahoo.com", 200, "ok"), ("getcrumb", 200, "CRUMB_A"),
         ("quoteSummary/AAPL", 429, "slow down")],
        [("fc.yahoo.com", 200, "ok"), ("getcrumb", 200, "CRUMB_B"),
         ("quoteSummary/AAPL", 200, PROFILE)],
        now,
    )
    slept: list[float] = []
    client = YahooClient(lanes=pool, sleep=slept.append, backoff=0.5)

    assert client.profile("AAPL") is not None
    # A second address is available, so the 429 costs a lane rather than a wait.
    assert slept == []
    assert crumbs_used(seen_a) == ["CRUMB_A"]
    assert crumbs_used(seen_b) == ["CRUMB_B"]


def test_a_401_on_one_lane_leaves_the_other_lanes_crumb_alone():
    now = [0.0]
    pool, seen_a, seen_b = two_lanes(
        [("fc.yahoo.com", 200, "ok"), ("getcrumb", 200, "CRUMB_A"),
         ("quoteSummary/AAPL", 200, PROFILE),
         ("quoteSummary/GOOG", 401, UNAUTHORISED),
         ("fc.yahoo.com", 200, "ok"), ("getcrumb", 200, "CRUMB_A2"),
         ("quoteSummary/GOOG", 200, PROFILE)],
        [("fc.yahoo.com", 200, "ok"), ("getcrumb", 200, "CRUMB_B"),
         ("quoteSummary/MSFT", 200, PROFILE),
         ("quoteSummary/NVDA", 200, PROFILE)],
        now,
    )
    client = YahooClient(lanes=pool)
    for symbol in ("AAPL", "MSFT", "GOOG", "NVDA"):
        assert client.profile(symbol) is not None

    # Lane 1 re-handshook; lane 2 never noticed and kept its original crumb.
    assert client.crumb_fetches == 3
    assert crumbs_used(seen_a) == ["CRUMB_A", "CRUMB_A", "CRUMB_A2"]
    assert crumbs_used(seen_b) == ["CRUMB_B", "CRUMB_B"]


def test_a_crumb_failure_on_one_lane_still_stops_the_run():
    # A handshake failing on a paid exit almost certainly means Yahoo is blocking
    # that address, and carrying on at half throughput would hide it.
    now = [0.0]
    pool, _, _ = two_lanes(
        [("fc.yahoo.com", 200, "ok"), ("getcrumb", 500, "")],
        [("fc.yahoo.com", 200, "ok"), ("getcrumb", 200, "CRUMB_B")],
        now,
    )
    with pytest.raises(CrumbUnavailable, match="lane-1"):
        YahooClient(lanes=pool).profile("AAPL")
