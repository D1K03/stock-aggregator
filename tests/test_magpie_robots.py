"""robots.txt, honoured rather than consulted.

`CLAUDE.md` requires respecting it and until now nothing did — it was a policy a
person applied when choosing a source. These defend the mechanical version.
"""

import httpx
import pytest

from screener.magpie import robots


@pytest.fixture(autouse=True)
def _no_cached_rules():
    robots.forget()
    yield
    robots.forget()


def answering(body: str, *, status: int = 200, count: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if count is not None:
            count.append(str(request.url))
        if status != 200:
            return httpx.Response(status, text="")
        return httpx.Response(200, text=body)

    return httpx.MockTransport(handler)


def test_robots_is_read_through_the_fetch_chain_and_not_by_the_stdlib(monkeypatch):
    # `RobotFileParser.read()` opens its own socket: no timeout we control, no
    # redaction on the error, and a urllib User-Agent that a good number of CDNs
    # answer with 403 — which the parser then reads as "disallow everything".
    # The module that decides whether to fetch must not be the one that fetches
    # in a way nothing else in the project can see.
    import urllib.robotparser

    def explode(self):
        raise AssertionError("robots.txt was read by the stdlib, not by screener.fetch")

    monkeypatch.setattr(urllib.robotparser.RobotFileParser, "read", explode)
    rules = robots.rules(
        "https://example.com/a",
        user_agent="MagpieBot",
        on_request=None,
        transport=answering("User-agent: *\nAllow: /"),
    )
    assert rules.allowed


def test_a_disallowed_path_is_not_allowed():
    rules = robots.rules(
        "https://example.com/private/x",
        user_agent="MagpieBot",
        on_request=None,
        transport=answering("User-agent: *\nDisallow: /private/"),
    )
    assert rules.allowed is False


def test_an_empty_robots_file_allows_rather_than_being_treated_as_a_failure():
    # An empty file is a real answer meaning "everything". `screener.fetch`
    # treats an empty 2xx as a failure by default, so this is only right because
    # the read passes allow_empty.
    rules = robots.rules("https://example.com/a", user_agent="MagpieBot",
                         transport=answering(""))
    assert rules.allowed


def test_a_site_with_no_robots_file_allows():
    # A 404 is by far the most common case and the standard reads absence as
    # permission. Refusing here would make most of the web unreachable.
    rules = robots.rules("https://example.com/a", user_agent="MagpieBot",
                         transport=answering("", status=404))
    assert rules.allowed


def test_a_crawl_delay_the_host_asks_for_is_read_back():
    rules = robots.rules(
        "https://example.com/a",
        user_agent="MagpieBot",
        on_request=None,
        transport=answering("User-agent: *\nCrawl-delay: 5\nDisallow:"),
    )
    assert rules.crawl_delay == 5.0


def test_one_host_is_asked_for_its_rules_once_and_then_remembered():
    # Scraping several pages from one site should be one robots fetch, not one
    # per page.
    seen: list[str] = []
    served = answering("User-agent: *\nDisallow: /private/", count=seen)
    for path in ("/a", "/b", "/c"):
        robots.rules(f"https://example.com{path}", user_agent="MagpieBot", on_request=None, transport=served)
    assert len(seen) == 1


def test_rules_for_a_named_agent_beat_the_general_ones():
    body = "User-agent: *\nDisallow:\n\nUser-agent: MagpieBot\nDisallow: /\n"
    served = answering(body)
    assert robots.rules("https://e.com/a", user_agent="MagpieBot", on_request=None, transport=served).allowed is False
    robots.forget()
    assert robots.rules("https://e.com/a", user_agent="Other", on_request=None, transport=served).allowed is True


def test_something_that_is_not_a_web_address_is_refused():
    assert robots.rules("file:///etc/passwd", user_agent="MagpieBot").allowed is False
