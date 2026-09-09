"""The ladder, and the three places it deliberately stops.

Everywhere else in this project the answer to a failed fetch is "try the next
strategy". Here there are three cases where escalating would be worse than
failing, and each of them is a test.
"""

import httpx
import pytest

from screener.fetch.result import FetchError
from screener.magpie import robots
from screener.magpie.acquire import NotAPage, NotPermitted, acquire, ladder
from screener.magpie.config import MagpieConfig

ARTICLE = "<html><head><title>T</title></head><body><article><p>{}</p></article></body></html>".format(
    " ".join(f"word{i}" for i in range(200))
)
CHALLENGE = "<html><body><h1>Just a moment...</h1>" + ("<div>padding</div>" * 80) + "</body></html>"

FREE = MagpieConfig(strategies=("direct", "isp_proxy", "unlocker"))


@pytest.fixture(autouse=True)
def _no_cached_rules():
    robots.forget()
    yield
    robots.forget()


def transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def serving(*, robots_body: str = "", page: str = ARTICLE, code: int = 200):
    """A site that answers robots.txt and one article."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots_body)
        return httpx.Response(code, text=page)

    return transport(handler)


def test_the_ladder_stops_at_the_first_strategy_that_answers():
    fetched = acquire("https://example.com/a", FREE, on_request=None, transport=serving())
    assert fetched.strategy == "direct"
    assert fetched.attempts == ("direct",)
    assert fetched.cost_usd == 0


def test_a_disallowed_url_is_refused_and_never_escalated(monkeypatch):
    # The one that keeps "respect robots.txt" from becoming "route around
    # robots.txt". Reaching for a residential proxy to fetch a page a site asked
    # us not to is worse than not having the rule at all.
    reached = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/")
        reached.append(str(request.url))
        return httpx.Response(200, text=ARTICLE)

    with pytest.raises(NotPermitted):
        acquire("https://example.com/private/a", FREE, on_request=None, transport=transport(handler))
    # Not merely "did not escalate" — the page was never requested at all.
    assert reached == []


def test_a_path_the_site_allows_is_fetched():
    served = serving(robots_body="User-agent: *\nDisallow: /private/")
    assert acquire("https://example.com/public/a", FREE, on_request=None, transport=served).strategy


def test_a_file_is_refused_before_any_request_is_made():
    # Paying the billed rung to fail on a PDF is the outcome this avoids.
    def explode(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a request was made for a file")

    with pytest.raises(NotAPage, match="file"):
        acquire("https://example.com/report.pdf", FREE, on_request=None, transport=transport(explode))


def test_something_that_is_not_a_web_address_is_refused():
    with pytest.raises(NotAPage):
        acquire("file:///etc/passwd", FREE)


def test_the_billed_rung_is_dropped_when_the_meter_says_so():
    # A capped scrape still tries both free strategies, which is why this meter
    # can fail closed without refusing anybody — unlike the chat spend cap,
    # where failing closed means silence.
    assert ladder(FREE, may_pay=True) == ("direct", "isp_proxy", "unlocker")
    assert ladder(FREE, may_pay=False) == ("direct", "isp_proxy")


def test_dropping_the_billed_rung_never_leaves_an_empty_ladder():
    only_paid = MagpieConfig(strategies=("unlocker",))
    assert ladder(only_paid, may_pay=False) == ("direct",)


def test_a_billed_fetch_reports_what_it_cost():
    from screener.magpie.acquire import Fetched
    from screener.magpie.config import UNLOCKER_USD

    assert Fetched("h", "u", "unlocker", ("direct", "unlocker"), 200).cost_usd == UNLOCKER_USD
    # An ISP lane is bandwidth against a flat monthly plan, so its marginal cost
    # really is zero rather than merely small.
    assert Fetched("h", "u", "isp_proxy", ("direct", "isp_proxy"), 200).cost_usd == 0


def test_a_page_that_says_it_is_not_free_to_read_is_refused_rather_than_paid_for():
    # "The same bytes a browser gets" is what justifies sending a browser
    # User-Agent. Paying a proxy network past a subscription check is not that.
    paywalled = (
        '<html><head><script type="application/ld+json">'
        '{"@type":"NewsArticle","isAccessibleForFree":false}</script></head>'
        "<body><article><p>" + " ".join(["word"] * 200) + "</p></article></body></html>"
    )
    with pytest.raises(NotAPage, match="free to read"):
        acquire("https://example.com/a", FREE, on_request=None, transport=serving(page=paywalled))


def test_a_soft_block_on_the_cheap_route_escalates_to_the_next(monkeypatch):
    # The behaviour the whole feature is for. A challenge page arrives as a 200
    # with a body, so without the `validate` hook the chain stops at the first
    # rung, stores an interstitial as an article, and never tries the proxy that
    # would have worked.
    monkeypatch.setenv("BRIGHTDATA_PROXY_HOST", "brd.example")
    monkeypatch.setenv("BRIGHTDATA_PROXY_PORT", "22225")
    monkeypatch.setenv("BRIGHTDATA_PROXY_USER", "user")
    monkeypatch.setenv("BRIGHTDATA_PROXY_PASS", "pass")

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        # The mock cannot tell us which strategy is calling, so the first page
        # request is the direct one and the second is the proxied one.
        seen.append("page")
        if len(seen) == 1:
            return httpx.Response(200, text=CHALLENGE)
        return httpx.Response(200, text=ARTICLE)

    fetched = acquire("https://example.com/a", FREE, on_request=None, transport=transport(handler))
    assert fetched.strategy == "isp_proxy"
    assert fetched.attempts == ("direct", "isp_proxy")
    # And it stopped there: the billed rung was never reached.
    assert "unlocker" not in fetched.attempts
    assert fetched.cost_usd == 0


def test_every_route_failing_raises_rather_than_storing_a_block_page(monkeypatch):
    monkeypatch.setenv("BRIGHTDATA_PROXY", "brd.example:22225:user:pass")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        return httpx.Response(200, text=CHALLENGE)

    with pytest.raises(FetchError):
        acquire("https://example.com/a", MagpieConfig(strategies=("direct", "isp_proxy")),
                transport=transport(handler))


def test_a_query_string_is_not_echoed_when_every_route_fails(monkeypatch):
    # API keys travel as query parameters, and `screener.fetch` already redacts
    # them. This asserts magpie did not undo that by formatting its own message.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        return httpx.Response(500)

    with pytest.raises(FetchError) as raised:
        acquire(
            "https://example.com/a?token=hunter2",
            MagpieConfig(strategies=("direct",)),
            transport=transport(handler),
        )
    assert "hunter2" not in str(raised.value)
