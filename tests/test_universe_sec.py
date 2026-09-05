import json

import httpx
import pytest

from screener.universe.sources.sec import cik_by_symbol, user_agent

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


@pytest.fixture(autouse=True)
def contact(monkeypatch):
    """SEC refuses a request that names nobody, so every call needs this set."""
    monkeypatch.setenv("SEC_CONTACT_EMAIL", "contact@example.com")


def test_the_user_agent_carries_the_contact_address_sec_demands():
    # The previous version of this test asserted only that the header said
    # "screener", which it did while every real request came back 403.
    transport, seen = recording_transport(PAYLOAD)
    cik_by_symbol(transport=transport)
    assert "contact@example.com" in seen[0].headers.get("user-agent", "")


def test_the_user_agent_never_names_github_because_sec_refuses_it():
    transport, seen = recording_transport(PAYLOAD)
    cik_by_symbol(transport=transport)
    assert "github.com" not in seen[0].headers.get("user-agent", "").lower()


def test_an_unset_contact_address_fails_by_name_rather_than_as_a_403(monkeypatch):
    # Defaulting to a placeholder would be answered with a 403 that reads as a
    # network fault, which is exactly how this went unnoticed before.
    monkeypatch.delenv("SEC_CONTACT_EMAIL", raising=False)
    with pytest.raises(RuntimeError, match="SEC_CONTACT_EMAIL is not set"):
        user_agent()


def test_a_github_contact_address_is_refused_locally(monkeypatch):
    monkeypatch.setenv("SEC_CONTACT_EMAIL", "someone@github.com")
    with pytest.raises(RuntimeError, match="must not contain"):
        user_agent()
