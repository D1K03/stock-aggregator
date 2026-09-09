"""Where the scraper is allowed to go.

Magpie is the only fetcher here whose destination is somebody else's input, and
the body it fetches is stored and shown back. That combination is what turns
"it made a request" into "it read something", so the address is checked rather
than trusted.

The test that matters most is the redirect one. Validating the URL somebody
submitted checks an address nobody fetches: a public page can answer with a
Location header and the request goes wherever it says.
"""

import httpx
import pytest

from screener.magpie import reachable
from screener.magpie.acquire import NotPermitted, acquire
from screener.magpie.config import MagpieConfig
from screener.magpie.reachable import NotReachable, check, guard

FREE = MagpieConfig(strategies=("direct",))
ARTICLE = "<html><head><title>T</title></head><body><article><p>{}</p></article></body></html>".format(
    " ".join(f"word{i}" for i in range(200))
)


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",   # the cloud metadata address
        "http://127.0.0.1:8080/status",
        "http://[::1]:8080/status",
        "http://10.0.0.5/admin",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://0.0.0.0/",
        "http://localhost/status",
    ],
)
def test_an_address_only_this_network_can_see_is_refused(url):
    with pytest.raises(NotReachable):
        check(url)


def test_an_ordinary_public_address_is_allowed():
    # The check has to let the web through, or the scraper scrapes nothing.
    check("https://example.com/an-article")


def test_a_name_that_does_not_resolve_is_refused():
    # Including every internal compose hostname, which resolves inside the
    # network and nowhere else.
    with pytest.raises(NotReachable):
        check("http://api:8080/status")


@pytest.mark.parametrize("host", ["0x7f.1", "2130706433", "127.1"])
def test_a_loopback_address_written_oddly_is_still_loopback(host):
    # `127.1` and `2130706433` are both the loopback address to a socket, and
    # writing one of those instead of 127.0.0.1 is the first thing anybody
    # tries. Whether it resolves or is rejected outright, it must not be
    # fetched.
    with pytest.raises(NotReachable):
        check(f"http://{host}/")


def test_every_address_a_name_resolves_to_has_to_be_public(monkeypatch):
    # A name answering with one public address and one private one is the
    # ordinary way this is got around. Taking the first answer would let it
    # through whenever the resolver happened to order them the other way.
    monkeypatch.setattr(reachable, "addresses", lambda host: ["93.184.216.34", "127.0.0.1"])
    with pytest.raises(NotReachable):
        check("http://mixed.example/")


def test_the_refusal_names_the_address_and_not_the_url():
    # A caller logs this, and `screener.fetch` redacts query strings because an
    # API key travels in one often enough to matter.
    with pytest.raises(NotReachable) as raised:
        check("http://127.0.0.1/path?token=hunter2")
    assert "hunter2" not in str(raised.value)


def test_the_hook_refuses_the_same_addresses_the_check_does():
    with pytest.raises(NotReachable):
        guard(httpx.Request("GET", "http://169.254.169.254/latest/meta-data/"))
    guard(httpx.Request("GET", "https://example.com/a"))


# -- the one that matters ---------------------------------------------------


def test_a_redirect_to_an_internal_address_is_refused_mid_flight():
    # The submitted URL is public and passes every check made before the
    # request. Without a hook on each hop, the client follows the Location
    # header into the private network and stores whatever comes back under the
    # public address that was asked for.
    reached: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        reached.append(str(request.url))
        if request.url.host == "example.com" and request.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"Location": "http://127.0.0.1:8080/status"})
        return httpx.Response(200, text=ARTICLE)

    with pytest.raises(Exception) as raised:
        acquire("https://example.com/a", FREE, transport=httpx.MockTransport(handler))

    # It was refused, and the internal address was never requested.
    assert not any("127.0.0.1" in url for url in reached)
    assert "example.com" in " ".join(reached)
    assert "NotReachable" in type(raised.value).__name__ or "FetchError" in type(raised.value).__name__


def test_a_private_address_submitted_directly_is_refused_before_any_request():
    def explode(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"a request was made to {request.url}")

    with pytest.raises(NotPermitted):
        acquire("http://169.254.169.254/latest/meta-data/", FREE,
                transport=httpx.MockTransport(explode))


def test_robots_is_not_fetched_from_an_internal_address():
    # robots.txt is read before the URL is otherwise permitted, so without the
    # same guard a refused scrape still reaches whatever it was pointed at.
    def explode(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"robots.txt was fetched from {request.url}")

    with pytest.raises(NotPermitted):
        acquire("http://127.0.0.1:8080/anything", FREE,
                transport=httpx.MockTransport(explode))


def test_the_guard_is_on_by_default_everywhere_that_fetches():
    # The switch is a parameter, and only tests ever pass it. That is exactly
    # the shape a protection quietly loses: somebody adds a caller, copies a
    # test's `on_request=None`, and nothing is red. Pinned by signature so the
    # default cannot drift to None without this failing.
    import inspect

    from screener.magpie import acquire as acquire_module
    from screener.magpie import robots as robots_module

    for function in (acquire_module.acquire, robots_module.rules):
        default = inspect.signature(function).parameters["on_request"].default
        assert default is guard, f"{function.__name__} no longer guards by default"


def test_the_scrape_path_refuses_a_private_address_without_a_request(fresh_db, tmp_path):
    # End to end through `run.scrape`, which is what the API and the tool both
    # call. The guard being on in `acquire` is only useful if the path that
    # actually runs goes through it.
    #
    # An attempt row is opened before the fetch, on purpose, so a process that
    # dies mid-fetch leaves a record. The refusal is what must never reach the
    # network.
    from screener.blobs import LocalStore
    from screener.magpie.run import scrape

    def explode(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"a request was made to {request.url}")

    outcome = scrape(
        "http://169.254.169.254/latest/meta-data/",
        conn=fresh_db,
        blobs=LocalStore(tmp_path),
        transport=httpx.MockTransport(explode),
    )
    assert outcome.document is None
    assert outcome.refused is not None and outcome.refused.reason == "robots"

    # And it is on the record as refused, so the same address is not tried again.
    with fresh_db.cursor() as cur:
        cur.execute("select state, reason from magpie.attempt order by id desc limit 1")
        assert cur.fetchone() == ("refused", "robots")
