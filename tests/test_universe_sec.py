import json

import httpx

from screener.universe.sources.sec import cik_by_symbol

PAYLOAD = json.dumps(
    {
        "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
        "1": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
        "2": {"cik_str": 1067983, "ticker": "BRK-B", "title": "BERKSHIRE HATHAWAY"},
    }
)


def recording_transport(body: str, status: int = 200):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, text=body)

    return httpx.MockTransport(handler), seen


def test_maps_symbol_to_zero_padded_cik():
    transport, _ = recording_transport(PAYLOAD)
    assert cik_by_symbol(transport=transport)["AAPL"] == "0000320193"


def test_cik_is_always_ten_digits():
    transport, _ = recording_transport(PAYLOAD)
    assert all(len(v) == 10 for v in cik_by_symbol(transport=transport).values())


def test_symbols_are_normalised():
    transport, _ = recording_transport(PAYLOAD)
    assert "BRK-B" in cik_by_symbol(transport=transport)


def test_a_user_agent_is_declared_because_sec_returns_403_without_one():
    transport, seen = recording_transport(PAYLOAD)
    cik_by_symbol(transport=transport)
    agent = seen[0].headers.get("user-agent", "")
    assert agent and "screener" in agent.lower()
