import csv
from pathlib import Path

import pytest

from screener.universe.refresh import refresh
from screener.universe.sources.wikipedia import Constituent
from screener.universe.sources.yahoo import Profile


class FakeClient:
    """Stands in for YahooClient; returns a profile for known symbols only."""

    def __init__(self, known: dict[str, Profile]) -> None:
        self.known = known
        self.asked: list[str] = []

    def profile(self, symbol: str) -> Profile | None:
        self.asked.append(symbol)
        return self.known.get(symbol)


PROFILE = Profile(sector="Technology", industry="Consumer Electronics", mic="XNAS", currency="USD")


@pytest.fixture
def sources(monkeypatch):
    monkeypatch.setattr(
        "screener.universe.refresh.constituents",
        lambda **_: [
            Constituent("AAPL", "Apple Inc.", "Information Technology", "sp500"),
            Constituent("CWEN-A", "Clearway A", "Utilities", "sp400"),
        ],
    )
    monkeypatch.setattr(
        "screener.universe.refresh.cik_by_symbol",
        lambda **_: {"AAPL": "0000320193"},
    )


def test_resolved_rows_go_to_universe_csv(tmp_path: Path, sources):
    report = refresh(tmp_path, client=FakeClient({"AAPL": PROFILE}))
    assert report.written == 1
    rows = list(csv.DictReader((tmp_path / "universe.csv").open()))
    assert rows[0]["symbol"] == "AAPL"
    assert rows[0]["yf_sector"] == "Technology"
    assert rows[0]["cik"] == "0000320193"
    assert rows[0]["gics_sector"] == "Information Technology"


def test_unresolved_rows_go_to_their_own_file_with_a_reason(tmp_path: Path, sources):
    report = refresh(tmp_path, client=FakeClient({"AAPL": PROFILE}))
    assert report.unresolved == 1
    rows = list(csv.DictReader((tmp_path / "universe-unresolved.csv").open()))
    assert rows[0]["symbol"] == "CWEN-A"
    assert rows[0]["reason"]


def test_the_main_file_never_contains_a_half_populated_row(tmp_path: Path, sources):
    refresh(tmp_path, client=FakeClient({"AAPL": PROFILE}))
    for row in csv.DictReader((tmp_path / "universe.csv").open()):
        assert all(row[field] for field in ("symbol", "mic", "currency", "yf_sector"))


def test_a_missing_cik_is_empty_rather_than_fatal(tmp_path: Path, sources, monkeypatch):
    monkeypatch.setattr("screener.universe.refresh.cik_by_symbol", lambda **_: {})
    refresh(tmp_path, client=FakeClient({"AAPL": PROFILE}))
    rows = list(csv.DictReader((tmp_path / "universe.csv").open()))
    assert rows[0]["cik"] == ""


def test_refresh_never_touches_the_database(tmp_path: Path, sources, monkeypatch):
    def explode():
        raise AssertionError("refresh must not open a database connection")

    monkeypatch.setattr("screener.config.settings", explode)
    refresh(tmp_path, client=FakeClient({"AAPL": PROFILE}))


def test_the_delay_is_applied_between_symbols(tmp_path: Path, sources):
    slept: list[float] = []
    refresh(tmp_path, client=FakeClient({"AAPL": PROFILE}), delay=0.25, sleep=slept.append)
    assert slept == [0.25, 0.25]
