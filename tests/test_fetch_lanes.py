import threading
import time

import httpx
import pytest

from screener.fetch import DIRECT_LANE, Lane, LanePool, StrategyUnavailable

URL = "https://example.test/data"


@pytest.fixture
def bright_data(monkeypatch):
    """Credentials and four exits, so a pool is reachable when asked for."""
    monkeypatch.setenv("BRIGHTDATA_PROXY", "brd.superproxy.io:44445:user:pass")
    monkeypatch.setenv(
        "BRIGHTDATA_PROXY_IPS", "10.0.0.1, 10.0.0.2 ,10.0.0.3,10.0.0.4"
    )


def responder(*responses):
    """A transport replaying `responses` in order, recording every request."""
    seen: list[httpx.Request] = []
    remaining = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return remaining.pop(0) if remaining else httpx.Response(200, text="tail")

    return httpx.MockTransport(handler), seen


def clock_at(now: list[float]):
    """A clock a test can wind forward by mutating `now`."""
    return lambda: now[0]


def lanes_at(count, now, transport=None):
    return LanePool(
        [
            Lane(f"lane-{i}", proxy=f"http://p{i}", transport=transport, clock=clock_at(now))
            for i in range(1, count + 1)
        ]
    )


def test_a_lane_pool_needs_at_least_one_lane():
    # Same refusal `fetch()` makes for an empty strategy list: nowhere to send a
    # request is a configuration mistake, not an empty collection.
    with pytest.raises(ValueError):
        LanePool([])


def test_a_direct_pool_never_reads_bright_data_credentials(monkeypatch, bright_data):
    # The promise is the one DEFAULT_STRATEGIES makes for fetch(): credentials are
    # fully configured here, and a pool built as direct must still not touch them.
    def refuse(*args, **kwargs):
        raise AssertionError("a direct pool read Bright Data configuration")

    monkeypatch.setattr("screener.fetch.lanes.ProxyConfig.from_env", refuse)

    transport, seen = responder(httpx.Response(200, text="ok"))
    with LanePool.direct(transport=transport) as pool:
        assert pool.names == (DIRECT_LANE,)
        assert pool.acquire().get(URL).status_code == 200
    assert len(seen) == 1


def test_from_env_declines_when_no_exits_are_configured(monkeypatch):
    for name in ("BRIGHTDATA_PROXY", "BRIGHTDATA_PROXY_IPS"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(StrategyUnavailable):
        LanePool.from_env()


def test_from_env_falls_back_to_one_direct_lane_when_asked(monkeypatch):
    # The Yahoo path starts on the proxy, so an unconfigured machine has to get
    # today's single direct client rather than an error.
    for name in ("BRIGHTDATA_PROXY", "BRIGHTDATA_PROXY_IPS"):
        monkeypatch.delenv(name, raising=False)
    with LanePool.from_env(fallback_to_direct=True) as pool:
        assert pool.names == (DIRECT_LANE,)


def test_from_env_builds_one_lane_per_configured_exit(bright_data):
    with LanePool.from_env() as pool:
        assert len(pool) == 4
        assert pool.names == ("lane-1", "lane-2", "lane-3", "lane-4")


def test_acquire_visits_every_lane_once_before_repeating():
    now = [0.0]
    with lanes_at(3, now) as pool:
        order = [pool.acquire().name for _ in range(6)]
    assert order == ["lane-1", "lane-2", "lane-3", "lane-1", "lane-2", "lane-3"]


def test_a_parked_lane_is_skipped_and_the_next_one_is_handed_out():
    now = [0.0]
    with lanes_at(2, now) as pool:
        first = pool.acquire()
        first.park(60)
        assert [pool.acquire().name for _ in range(2)] == ["lane-2", "lane-2"]


def test_a_lane_comes_back_when_its_cooldown_expires():
    now = [0.0]
    with lanes_at(2, now) as pool:
        pool.acquire().park(60)
        assert pool.acquire().name == "lane-2"
        now[0] += 61
        assert "lane-1" in {pool.acquire().name for _ in range(2)}


def test_when_every_lane_is_parked_the_pool_hands_back_the_one_that_frees_soonest():
    now = [0.0]
    with lanes_at(2, now) as pool:
        a, b = pool.acquire(), pool.acquire()
        a.park(60)
        b.park(10)
        chosen = pool.acquire()
    assert chosen.name == b.name
    assert chosen.parked_for == pytest.approx(10)


def test_a_fully_parked_pool_never_sleeps_or_retries_on_its_own():
    # The D6 guard. Choosing a lane is this layer's job; waiting and re-issuing
    # belong to the caller, so acquire() must not advance the clock or send.
    now = [0.0]
    transport, seen = responder()
    with lanes_at(2, now, transport=transport) as pool:
        pool.acquire().park(60)
        pool.acquire().park(60)
        pool.acquire()
    assert now[0] == 0.0
    assert seen == []


def test_two_lanes_do_not_share_a_cookie_jar():
    # A lane is a jar, which is the whole reason it exists. One transport each,
    # so the only way a cookie crosses is if the clients share state.
    def jar(value):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/set":
                return httpx.Response(200, headers={"set-cookie": f"who={value}"})
            return httpx.Response(200, text=request.headers.get("cookie", ""))

        return httpx.MockTransport(handler)

    a = Lane("lane-1", proxy="http://p1", transport=jar("a"))
    b = Lane("lane-2", proxy="http://p2", transport=jar("b"))
    with a, b:
        a.get("https://example.test/set")
        b.get("https://example.test/set")
        assert a.get("https://example.test/read").text == "who=a"
        assert b.get("https://example.test/read").text == "who=b"


def test_a_direct_lane_keeps_tls_verification_on(monkeypatch):
    # Bright Data terminates TLS on its own chain, so a proxied lane cannot
    # verify against the origin hostname. That exemption must not leak onto the
    # default path, which is the one carrying everything today.
    #
    # Asserted against the argument rather than the client httpx builds from it:
    # where the verify setting ends up is an httpx implementation detail that has
    # moved between releases, and the decision is what this guards.
    built: list[dict] = []
    real = httpx.Client

    def record(**kwargs):
        built.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr("screener.fetch.lanes.httpx.Client", record)
    Lane(DIRECT_LANE).close()
    Lane("lane-1", proxy="http://p1").close()

    assert built[0]["proxy"] is None and built[0]["verify"] is True
    assert built[1]["proxy"] == "http://p1" and built[1]["verify"] is False


def test_a_mock_transport_wins_over_a_proxy():
    # httpx builds proxy *mounts*, and a mount takes precedence over an explicit
    # transport — so without the guard in Lane every test here would try to dial
    # brd.superproxy.io for real.
    transport, seen = responder(httpx.Response(200, text="local"))
    with Lane("lane-1", proxy="http://p1", transport=transport) as lane:
        assert lane.get(URL).text == "local"
    assert len(seen) == 1


def test_a_lane_never_raises_on_status():
    # Yahoo says "your crumb expired" with a 401, so the status has to survive.
    transport, _ = responder(httpx.Response(401, text="Invalid Crumb"))
    with Lane(DIRECT_LANE, transport=transport) as lane:
        assert lane.get(URL).status_code == 401


def test_closing_the_pool_closes_every_lane():
    now = [0.0]
    pool = lanes_at(3, now)
    lanes = [pool.acquire() for _ in range(3)]
    pool.close()
    assert all(lane._client.is_closed for lane in lanes)


# -- running across the lanes ----------------------------------------------


def test_across_gives_back_results_in_the_order_it_was_given():
    now = [0.0]
    with lanes_at(4, now) as pool:
        got = pool.across(list(range(20)), lambda lane, n: n * 2)
    assert got == [n * 2 for n in range(20)]


def test_across_never_runs_more_workers_than_there_are_lanes():
    # The whole safety claim is "one request in flight per exit address", not
    # "concurrency is fine". If this bound ever slips, four addresses stop
    # meaning anything and the pool is just four workers on one IP again.
    now = [0.0]
    live = 0
    peak = 0
    lock = threading.Lock()

    def work(lane, n):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.01)
        with lock:
            live -= 1
        return n

    with lanes_at(3, now) as pool:
        pool.across(list(range(30)), work)
    assert peak <= 3


def test_across_never_hands_one_lane_to_two_workers_at_once():
    # A lane is one client and one cookie jar. Two threads inside it is a race,
    # and with a crumb in that jar it is a race that returns someone else's 401.
    now = [0.0]
    inside: dict[str, int] = {}
    clash = []
    lock = threading.Lock()

    def work(lane, n):
        with lock:
            inside[lane.name] = inside.get(lane.name, 0) + 1
            if inside[lane.name] > 1:
                clash.append(lane.name)
        time.sleep(0.005)
        with lock:
            inside[lane.name] -= 1
        return n

    with lanes_at(4, now) as pool:
        pool.across(list(range(40)), work)
    assert clash == []


def test_across_a_single_lane_pool_is_simply_sequential():
    # The unconfigured case, and it must stay the behaviour it always had.
    now = [0.0]
    order = []
    with LanePool.direct() as pool:
        pool.across(list(range(6)), lambda lane, n: order.append(n))
    assert order == list(range(6))


def test_across_nothing_does_nothing():
    now = [0.0]
    with lanes_at(2, now) as pool:
        assert pool.across([], lambda lane, n: n) == []


def test_across_raises_a_failure_rather_than_returning_half_a_run():
    # Losing one item silently would put a gap in a nightly ingest that nothing
    # downstream could tell from a ticker Yahoo does not carry.
    now = [0.0]

    def work(lane, n):
        if n == 7:
            raise RuntimeError("that one broke")
        return n

    with lanes_at(3, now) as pool:
        with pytest.raises(RuntimeError, match="that one broke"):
            pool.across(list(range(20)), work)
