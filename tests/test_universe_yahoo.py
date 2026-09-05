import json

import httpx

from screener.universe.sources.yahoo import YahooClient

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
