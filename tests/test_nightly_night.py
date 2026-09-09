"""One night: the order, the gate, and what counts as done.

The gate is the point. `cutoff_offset` filters on `observed_at`, so yesterday's
bars stay visible -- scoring after a dead ingest would write a
complete-looking snapshot from stale data, and tomorrow's crossing diff would
read it as "nothing moved".
"""

import json
from datetime import date, timedelta

import pytest

from screener.nightly import NightReport, already_scored, run_night

TODAY = date(2026, 9, 15)


class FakeChart:
    """Bars for every symbol, or none at all.

    Shape matches `tests/conftest.py`'s `_chart_bytes` helper (the `meta` key
    and float-typed OHLC) because that is what the price parser actually
    expects -- a fake that skips `meta` parses as every security failing.
    """

    def __init__(self, *, answer: bool = True):
        self.answer = answer

    def fetch(self, symbol, start, end):
        if not self.answer:
            return None
        return json.dumps({
            "chart": {"result": [{
                "meta": {"currency": "USD"},
                "timestamp": [1757894400],
                "indicators": {"quote": [{
                    "open": [1.0], "high": [1.0], "low": [1.0],
                    "close": [1.0], "volume": [1],
                }]},
            }]}
        }).encode()


class FakeTimeseries:
    def __init__(self, *, answer: bool = True):
        self.answer = answer

    def fetch(self, symbol):
        if not self.answer:
            return None
        return json.dumps({"timeseries": {"result": []}}).encode()


class FakeBlobs:
    def __init__(self):
        self.written = {}

    def put(self, path, data):
        self.written[path] = data

    def get(self, path):
        return self.written[path]


@pytest.fixture
def two(fresh_db):
    # `screener.ingest.active_securities` (what `run_night` calls) joins
    # `security_symbol` for the current symbol, so a security needs one row
    # there too -- unlike `screener.scoring.active_securities`, which only
    # needs `security` itself.
    for name, symbol in (("Alpha", "AAA"), ("Beta", "BBB")):
        security_id = fresh_db.execute(
            """insert into security
               (name, mic, currency, country, primary_symbol, first_seen)
               values (%s, 'XNAS', 'USD', 'US', %s, '2020-01-01') returning id""",
            (name, symbol),
        ).fetchone()[0]
        fresh_db.execute(
            """insert into security_symbol
               (security_id, symbol, mic, valid_from, source)
               values (%s, %s, 'XNAS', '2020-01-01', 'yfinance')""",
            (security_id, symbol),
        )
    return fresh_db


def _scored(conn, day, outcome="ok", status="live"):
    logic = conn.execute(
        "select id from scoring_logic_version order by id limit 1"
    ).fetchone()[0]
    weight = conn.execute(
        "select id from weight_version where code = 'v1'"
    ).fetchone()[0]
    conn.execute(
        """insert into scoring_run
           (as_of_range, cutoff_offset, logic_version_id, weight_version_id,
            status, emits_alerts, git_sha, config_hash, started_at, outcome)
           values (daterange(%s, %s, '[)'), '1 day 6 hours', %s, %s,
                   %s, false, 'abc', '\\x00'::bytea, now(), %s)""",
        (day, day + timedelta(days=1), logic, weight, status, outcome),
    )


def test_a_night_with_nothing_scored_is_not_already_scored(fresh_db):
    assert already_scored(fresh_db, TODAY) is False


def test_a_successful_live_run_today_counts_as_scored(fresh_db):
    _scored(fresh_db, TODAY)
    assert already_scored(fresh_db, TODAY) is True


def test_a_failed_run_today_does_not_count_as_scored(fresh_db):
    # Otherwise the catch-up check would treat a lost night as a finished one.
    _scored(fresh_db, TODAY, outcome="failed")
    assert already_scored(fresh_db, TODAY) is False


def test_a_run_still_going_does_not_count_as_scored(fresh_db):
    _scored(fresh_db, TODAY, outcome="running")
    assert already_scored(fresh_db, TODAY) is False


def test_yesterdays_run_does_not_count_as_tonight(fresh_db):
    _scored(fresh_db, TODAY - timedelta(days=1))
    assert already_scored(fresh_db, TODAY) is False


def test_prices_wholly_failing_stops_scoring(two, monkeypatch):
    called = []
    monkeypatch.setattr(
        "screener.nightly.night.run_scoring",
        lambda *a, **k: called.append(k) or (_ for _ in ()).throw(AssertionError),
    )

    report = run_night(
        two, today=TODAY, blobs=FakeBlobs(),
        chart=FakeChart(answer=False), timeseries=FakeTimeseries(),
    )

    assert called == []
    assert report.scoring is None
    assert report.ok is False
    assert report.prices.status == "failed"


def test_a_partial_ingest_still_scores(two, monkeypatch):
    # CWEN-A fails every single night -- the only failure in 3,012 requests
    # across a measured night. A gate that stopped on any per-security failure
    # would stop every night.
    class OneBadSymbol(FakeChart):
        def fetch(self, symbol, start, end):
            return None if symbol == "BBB" else super().fetch(symbol, start, end)

    called = []
    monkeypatch.setattr(
        "screener.nightly.night.run_scoring",
        lambda *a, **k: called.append(k) or "scored",
    )

    report = run_night(
        two, today=TODAY, blobs=FakeBlobs(),
        chart=OneBadSymbol(), timeseries=FakeTimeseries(),
    )

    assert len(called) == 1
    assert report.prices.status == "partial"
    assert report.ok is True


def test_a_fundamentals_failure_does_not_stop_scoring(two, monkeypatch):
    # Scoring reads bars and nothing else this cycle: no ratio consumes a
    # fundamental fact yet, so a fundamentals failure must not block a run that
    # does not depend on it. This widens when the ratios cycle lands.
    called = []
    monkeypatch.setattr(
        "screener.nightly.night.run_scoring",
        lambda *a, **k: called.append(k) or "scored",
    )

    report = run_night(
        two, today=TODAY, blobs=FakeBlobs(),
        chart=FakeChart(), timeseries=FakeTimeseries(answer=False),
    )

    assert len(called) == 1
    assert report.fundamentals.status == "failed"
    assert report.ok is True


def test_the_counts_reach_the_log(two, monkeypatch, caplog):
    # Spec S7: a partial night is "counted and logged". The scheduler never
    # goes through `screener.ingest.cli`'s own aggregate line, so without one
    # here a night where most securities failed logs identically to a clean
    # one.
    class OneBadSymbol(FakeChart):
        def fetch(self, symbol, start, end):
            return None if symbol == "BBB" else super().fetch(symbol, start, end)

    monkeypatch.setattr(
        "screener.nightly.night.run_scoring", lambda *a, **k: "scored",
    )

    with caplog.at_level("INFO", logger="screener.nightly.night"):
        run_night(
            two, today=TODAY, blobs=FakeBlobs(),
            chart=OneBadSymbol(), timeseries=FakeTimeseries(answer=False),
        )

    messages = [r.message for r in caplog.records]
    assert any("prices" in m and "fundamentals" in m for m in messages)


def test_scoring_is_asked_for_todays_date(two, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "screener.nightly.night.run_scoring",
        lambda conn, **k: seen.update(k) or "scored",
    )

    run_night(
        two, today=TODAY, blobs=FakeBlobs(),
        chart=FakeChart(), timeseries=FakeTimeseries(),
    )

    assert seen["as_of"] == TODAY
