import httpx
import pytest

from screener.fetch import (
    DEFAULT_STRATEGIES,
    FetchError,
    StrategyUnavailable,
    fetch,
)

URL = "https://example.test/data"


@pytest.fixture
def bright_data(monkeypatch):
    """Credentials present, so a proxy strategy is reachable when asked for."""
    monkeypatch.setenv("BRIGHTDATA_PROXY", "brd.superproxy.io:44445:user:pass")
    monkeypatch.setenv("BRIGHTDATA_API_KEY", "an-api-key")
    monkeypatch.setenv("BRIGHTDATA_UNLOCKER_ZONE", "a-zone")


def responder(*responses):
    """A transport replaying `responses` in order, recording every request."""
    seen: list[httpx.Request] = []
    remaining = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return remaining.pop(0) if remaining else httpx.Response(200, text="tail")

    return httpx.MockTransport(handler), seen


def test_the_default_strategy_list_is_direct_only():
    assert DEFAULT_STRATEGIES == ("direct",)


def test_bright_data_is_never_reached_under_the_default_strategy_list(
    monkeypatch, bright_data
):
    # The promise DESIGN.md makes is that a proxy network sits outside the
    # ingest path unless something asks for it by name. Credentials are fully
    # configured here; the default must still not touch them.
    def refuse(*args, **kwargs):
        raise AssertionError("a proxy strategy was reached by default")

    monkeypatch.setitem(__import__("screener.fetch.chain", fromlist=["STRATEGIES"]).STRATEGIES, "isp_proxy", refuse)
    monkeypatch.setitem(__import__("screener.fetch.chain", fromlist=["STRATEGIES"]).STRATEGIES, "unlocker", refuse)

    transport, seen = responder(httpx.Response(200, text="ok"))
    result = fetch(URL, transport=transport)

    assert result.strategy == "direct"
    assert result.attempts == ("direct",)
    assert len(seen) == 1


def test_the_chain_falls_through_a_failure_and_records_what_it_tried(bright_data):
    transport, seen = responder(
        httpx.Response(403), httpx.Response(200, text='{"ok":true}')
    )
    result = fetch(URL, ("direct", "isp_proxy"), transport=transport)

    assert result.strategy == "isp_proxy"
    assert result.attempts == ("direct", "isp_proxy")
    assert result.json() == {"ok": True}
    assert len(seen) == 2


def test_the_chain_stops_at_the_first_success(bright_data):
    transport, seen = responder(httpx.Response(200, text="first"))
    result = fetch(URL, ("direct", "isp_proxy", "unlocker"), transport=transport)

    assert result.strategy == "direct"
    assert len(seen) == 1


def test_an_empty_two_hundred_is_rejected_and_escalates(bright_data):
    # More than one provider answers a rate-limited request with 200 and no
    # body, which is indistinguishable from "nothing to report". The fact layer
    # is append-only, so writing that as a real observation is unrecoverable.
    transport, seen = responder(
        httpx.Response(200, text="   "), httpx.Response(200, text="real")
    )
    result = fetch(URL, ("direct", "isp_proxy"), transport=transport)

    assert result.strategy == "isp_proxy"
    assert result.text == "real"


def test_allow_empty_lets_a_genuinely_empty_response_through():
    transport, _ = responder(httpx.Response(200, text=""))
    result = fetch(URL, transport=transport, allow_empty=True)
    assert result.text == ""


def test_a_validate_callback_can_demote_a_successful_response(bright_data):
    def reject_sentinel(result):
        if "rate limited" in result.text:
            raise RuntimeError("throttled")

    transport, _ = responder(
        httpx.Response(200, text="rate limited"), httpx.Response(200, text="real")
    )
    result = fetch(
        URL, ("direct", "isp_proxy"), validate=reject_sentinel, transport=transport
    )
    assert result.text == "real"


def test_an_unknown_strategy_name_is_collected_rather_than_raised():
    transport, _ = responder(httpx.Response(200, text="ok"))
    result = fetch(URL, ("typo", "direct"), transport=transport)
    assert result.strategy == "direct"


def test_when_every_strategy_fails_the_error_names_each_one(bright_data):
    transport, _ = responder(httpx.Response(500), httpx.Response(500))
    with pytest.raises(FetchError) as caught:
        fetch(URL, ("direct", "isp_proxy"), transport=transport)

    message = str(caught.value)
    assert "direct:" in message
    assert "isp_proxy:" in message


def test_a_query_string_is_not_echoed_into_the_error():
    # API keys travel as query parameters, and an exception message is one of
    # the most reliable ways to get one into a log aggregator.
    transport, _ = responder(httpx.Response(500))
    with pytest.raises(FetchError) as caught:
        fetch("https://example.test/d?token=super-secret", transport=transport)

    assert "super-secret" not in str(caught.value)


def test_a_proxy_strategy_without_credentials_declines_at_call_time(monkeypatch):
    for name in ("BRIGHTDATA_PROXY", "BRIGHTDATA_PROXY_USER", "BRIGHTDATA_PROXY_PASS"):
        monkeypatch.delenv(name, raising=False)
    from screener.fetch.strategies import isp_proxy

    with pytest.raises(StrategyUnavailable):
        isp_proxy(URL, 5.0, {}, None)
