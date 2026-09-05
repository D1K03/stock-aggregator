from datetime import date

import httpx

from screener.ingest.sweep import run_sweep

# `FakeClient` and `chart_bytes` come from conftest (Task 6): tests/ is not a
# package, so cross-importing between test modules is not reliable.

TODAY = date(2026, 9, 5)


def test_the_sweep_reports_a_mismatch_by_field(fresh_db, ingest_ctx, FakeClient, chart_bytes):
    sid, obs = ingest_ctx
    day = date(2026, 5, 1)
    fresh_db.execute(
        """insert into price_daily
           (security_id, trade_date, open, high, low, close, volume,
            observed_at, ingest_observation_id)
           values (%s, %s, 100, 100, 100, 100, 10, now(), %s)""",
        (sid, day, obs),
    )
    client = FakeClient({"AAA": chart_bytes(day, close="100", volume=99)})
    report = run_sweep(fresh_db, client=client, today=TODAY, securities=[(sid, "AAA")])
    assert report.by_field == {"volume": 1}
    assert report.compared == 1


def test_the_sweep_writes_nothing_at_all(fresh_db, ingest_ctx, FakeClient, chart_bytes):
    sid, obs = ingest_ctx
    day = date(2026, 5, 1)
    fresh_db.execute(
        """insert into price_daily
           (security_id, trade_date, open, high, low, close, volume,
            observed_at, ingest_observation_id)
           values (%s, %s, 100, 100, 100, 100, 10, now(), %s)""",
        (sid, day, obs),
    )
    before = (
        fresh_db.execute("select count(*) from price_daily").fetchone()[0],
        fresh_db.execute("select count(*) from ingest_observation").fetchone()[0],
        fresh_db.execute("select count(*) from ingest_run").fetchone()[0],
        fresh_db.execute("select close, volume from price_daily").fetchone(),
    )
    client = FakeClient({"AAA": chart_bytes(day, close="777", volume=999)})
    run_sweep(fresh_db, client=client, today=TODAY, securities=[(sid, "AAA")])
    after = (
        fresh_db.execute("select count(*) from price_daily").fetchone()[0],
        fresh_db.execute("select count(*) from ingest_observation").fetchone()[0],
        fresh_db.execute("select count(*) from ingest_run").fetchone()[0],
        fresh_db.execute("select close, volume from price_daily").fetchone(),
    )
    assert before == after


def test_a_bar_yahoo_returns_that_we_do_not_hold_is_not_a_mismatch(
    fresh_db, ingest_ctx, FakeClient, chart_bytes
):
    sid, _ = ingest_ctx
    client = FakeClient({"AAA": chart_bytes(date(2026, 5, 1))})
    report = run_sweep(fresh_db, client=client, today=TODAY, securities=[(sid, "AAA")])
    assert report.by_field == {}


class _RaisingClient:
    """A ChartClient-shaped stub where one symbol raises instead of returning None.

    `FakeClient` from conftest only ever returns bodies from a dict, with no way
    to raise, so this is defined locally rather than modifying the shared
    fixture (Task 8's tests depend on it).
    """

    def __init__(self, bodies):
        self.bodies = bodies

    def fetch(self, symbol, start, end):
        if symbol not in self.bodies:
            raise httpx.HTTPError("boom")
        return self.bodies[symbol]


def test_one_symbol_raising_does_not_take_down_the_sweep(
    fresh_db, two_securities, an_observation, chart_bytes
):
    (sid_a, symbol_a), (sid_b, symbol_b) = two_securities
    obs = an_observation(sid_b)
    day = date(2026, 5, 1)
    fresh_db.execute(
        """insert into price_daily
           (security_id, trade_date, open, high, low, close, volume,
            observed_at, ingest_observation_id)
           values (%s, %s, 100, 100, 100, 100, 10, now(), %s)""",
        (sid_b, day, obs),
    )
    client = _RaisingClient({symbol_b: chart_bytes(day, close="100", volume=10)})
    report = run_sweep(
        fresh_db, client=client, today=TODAY, securities=[(sid_a, symbol_a), (sid_b, symbol_b)]
    )
    # The raising security is skipped; the other security's bar is still compared.
    assert report.compared == 1
    assert report.by_field == {}
