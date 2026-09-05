import gzip
from datetime import date

import pytest

from screener.blobs import LocalStore
from screener.ingest.run import run_prices

TODAY = date(2026, 9, 5)


# `FakeClient` and `chart_bytes` come from conftest (Task 6).


def test_a_run_writes_an_observation_a_blob_and_the_bars(
    fresh_db, two_securities, tmp_path, FakeClient, chart_bytes
):
    (sid, symbol), _ = two_securities
    client = FakeClient({symbol: chart_bytes(date(2026, 9, 4))})
    blobs = LocalStore(tmp_path)
    report = run_prices(
        fresh_db, client=client, blobs=blobs, today=TODAY, securities=[(sid, symbol)]
    )
    assert report.ok == 1 and report.failed == 0
    assert fresh_db.execute(
        "select count(*) from price_daily where security_id=%s", (sid,)
    ).fetchone()[0] == 1
    path = fresh_db.execute(
        "select blob_path from ingest_observation where security_id=%s", (sid,)
    ).fetchone()[0]
    assert gzip.decompress(blobs.get(path))


def test_the_observation_and_both_fact_writes_are_one_transaction(
    fresh_db, two_securities, tmp_path, monkeypatch, FakeClient, chart_bytes
):
    # THE -50% MOMENTUM TEST. A run that inserts bars, dies, and leaves the
    # split un-inserted produces an unadjusted series across a 2-for-1 split:
    # a -50% twelve-month return that looks like real data rather than failure.
    (sid, symbol), _ = two_securities
    client = FakeClient({symbol: chart_bytes(date(2026, 9, 4), split=True)})

    import screener.ingest.run as run_module

    def explode(*args, **kwargs):
        raise RuntimeError("died between the bars and the split")

    monkeypatch.setattr(run_module, "insert_actions", explode)

    with pytest.raises(RuntimeError, match="died between"):
        run_prices(
            fresh_db, client=client, blobs=LocalStore(tmp_path), today=TODAY,
            securities=[(sid, symbol)],
        )

    assert fresh_db.execute(
        "select count(*) from price_daily where security_id=%s", (sid,)
    ).fetchone()[0] == 0
    assert fresh_db.execute(
        "select count(*) from ingest_observation where security_id=%s", (sid,)
    ).fetchone()[0] == 0


def test_one_securitys_failure_does_not_end_the_run(
    fresh_db, two_securities, tmp_path, FakeClient, chart_bytes
):
    (good, good_symbol), (bad, bad_symbol) = two_securities
    client = FakeClient({good_symbol: chart_bytes(date(2026, 9, 4))})  # bad returns None
    report = run_prices(
        fresh_db,
        client=client,
        blobs=LocalStore(tmp_path),
        today=TODAY,
        securities=[(good, good_symbol), (bad, bad_symbol)],
    )
    assert report.ok == 1 and report.failed == 1
    assert report.requested == 2


def test_an_empty_body_fails_that_security_without_ending_the_run(
    fresh_db, two_securities, tmp_path, FakeClient, chart_bytes
):
    # R10: a 200 with an empty body is a known Yahoo rate-limit tell, not real
    # data. It must be counted as a failure like `None`, must not write an
    # observation or a blob, and must not stop the other security's run.
    (good, good_symbol), (bad, bad_symbol) = two_securities
    client = FakeClient(
        {good_symbol: chart_bytes(date(2026, 9, 4)), bad_symbol: b""}
    )
    blobs = LocalStore(tmp_path)
    report = run_prices(
        fresh_db,
        client=client,
        blobs=blobs,
        today=TODAY,
        securities=[(good, good_symbol), (bad, bad_symbol)],
    )
    assert report.ok == 1 and report.failed == 1
    assert report.requested == 2
    assert fresh_db.execute(
        "select count(*) from ingest_observation where security_id=%s", (bad,)
    ).fetchone()[0] == 0
    from screener.blobs import BlobWriteFailed, blob_path

    with pytest.raises(BlobWriteFailed):
        blobs.get(blob_path("yahoo", "chart", TODAY, bad))


def test_a_blob_failure_aborts_the_run_and_writes_no_observation(
    fresh_db, two_securities, tmp_path, FakeClient, chart_bytes
):
    from screener.blobs import BlobWriteFailed

    (sid, symbol), _ = two_securities

    class Broken:
        def put(self, path, data):
            raise BlobWriteFailed("R2 is down")

        def get(self, path):
            raise BlobWriteFailed("R2 is down")

    client = FakeClient({symbol: chart_bytes(date(2026, 9, 4))})
    with pytest.raises(BlobWriteFailed):
        run_prices(
            fresh_db, client=client, blobs=Broken(), today=TODAY, securities=[(sid, symbol)]
        )
    assert fresh_db.execute("select count(*) from ingest_observation").fetchone()[0] == 0


def test_re_running_the_same_night_is_idempotent(
    fresh_db, two_securities, tmp_path, FakeClient, chart_bytes
):
    (sid, symbol), _ = two_securities
    blobs = LocalStore(tmp_path)
    for _ in range(2):
        client = FakeClient({symbol: chart_bytes(date(2026, 9, 4))})
        run_prices(
            fresh_db, client=client, blobs=blobs, today=TODAY, securities=[(sid, symbol)]
        )
    rows = fresh_db.execute(
        "select count(*) from price_daily where security_id=%s", (sid,)
    ).fetchone()[0]
    assert rows == 1


def test_a_second_run_asks_from_a_narrower_window(
    fresh_db, two_securities, tmp_path, FakeClient, chart_bytes
):
    (sid, symbol), _ = two_securities
    blobs = LocalStore(tmp_path)
    first = FakeClient({symbol: chart_bytes(date(2026, 9, 4))})
    run_prices(fresh_db, client=first, blobs=blobs, today=TODAY, securities=[(sid, symbol)])
    assert first.asked[0][1] == date(2020, 1, 1)

    second = FakeClient({symbol: chart_bytes(date(2026, 9, 4))})
    run_prices(fresh_db, client=second, blobs=blobs, today=TODAY, securities=[(sid, symbol)])
    assert second.asked[0][1] == date(2026, 8, 29)


def test_an_unchanged_payload_still_writes_an_observation(
    fresh_db, two_securities, tmp_path, FakeClient, chart_bytes
):
    (sid, symbol), _ = two_securities
    blobs = LocalStore(tmp_path)
    body = chart_bytes(date(2026, 9, 4))
    for _ in range(2):
        run_prices(
            fresh_db, client=FakeClient({symbol: body}), blobs=blobs, today=TODAY,
            securities=[(sid, symbol)],
        )
    observations = fresh_db.execute(
        "select count(*), count(*) filter (where is_new_payload) from ingest_observation"
    ).fetchone()
    assert observations[0] == 2      # always recorded
    assert observations[1] == 1      # blob written once


class _RaisingClient:
    """A ChartClient-shaped stub where a named symbol raises instead of returning.

    `FakeClient` from conftest only ever returns bodies from a dict, with no way
    to raise, so the failure boundary gets its own stub rather than a change to
    the shared fixture.
    """

    def __init__(self, bodies, raises=None):
        self.bodies = bodies
        self.raises = raises or {}

    def fetch(self, symbol, start, end):
        if symbol in self.raises:
            raise self.raises[symbol]
        return self.bodies.get(symbol)


def test_a_transport_error_that_is_not_an_httperror_fails_only_that_security(
    fresh_db, two_securities, tmp_path, chart_bytes
):
    # `httpx.InvalidURL` descends from `Exception`, not from `httpx.HTTPError`,
    # so `ChartClient._request` does not convert it to None and it reaches
    # `run_prices`. `active_securities` orders by symbol, so without a boundary
    # here a symbol that fails deterministically would block every
    # alphabetically later security on every subsequent night.
    import httpx

    (bad, bad_symbol), (good, good_symbol) = two_securities
    client = _RaisingClient(
        {good_symbol: chart_bytes(date(2026, 9, 4))},
        raises={bad_symbol: httpx.InvalidURL("not a url")},
    )
    report = run_prices(
        fresh_db,
        client=client,
        blobs=LocalStore(tmp_path),
        today=TODAY,
        securities=[(bad, bad_symbol), (good, good_symbol)],
    )
    assert report.failed == 1 and report.ok == 1
    assert fresh_db.execute(
        "select count(*) from price_daily where security_id=%s", (good,)
    ).fetchone()[0] == 1
    assert fresh_db.execute(
        "select count(*) from ingest_observation where security_id=%s", (bad,)
    ).fetchone()[0] == 0


def test_a_database_error_mid_security_rolls_that_one_back_and_the_run_continues(
    fresh_db, two_securities, tmp_path, monkeypatch, FakeClient, chart_bytes
):
    # Spec section 7: "database error mid-security -> that security's
    # transaction rolls back whole; run continues".
    import psycopg

    import screener.ingest.run as run_module

    (bad, bad_symbol), (good, good_symbol) = two_securities
    real = run_module.insert_settled_bars

    def flaky(cur, security_id, observation_id, bars, cutoff):
        if security_id == bad:
            raise psycopg.errors.DeadlockDetected("deadlock detected")
        return real(cur, security_id, observation_id, bars, cutoff)

    monkeypatch.setattr(run_module, "insert_settled_bars", flaky)

    client = FakeClient(
        {
            bad_symbol: chart_bytes(date(2026, 1, 5)),
            good_symbol: chart_bytes(date(2026, 1, 5)),
        }
    )
    report = run_prices(
        fresh_db,
        client=client,
        blobs=LocalStore(tmp_path),
        today=TODAY,
        securities=[(bad, bad_symbol), (good, good_symbol)],
    )
    assert report.failed == 1 and report.ok == 1
    # Rolled back whole: the observation went in first, and it is gone too.
    assert fresh_db.execute(
        "select count(*) from ingest_observation where security_id=%s", (bad,)
    ).fetchone()[0] == 0
    assert fresh_db.execute(
        "select count(*) from price_daily where security_id=%s", (good,)
    ).fetchone()[0] == 1


def test_an_unchanged_payload_across_a_date_boundary_points_at_a_blob_that_exists(
    fresh_db, two_securities, tmp_path, FakeClient, chart_bytes
):
    # `blob_path` is not null so that "every score traces back to the stored
    # response" is a claim the database enforces. A blob is only written when
    # the hash changes, so a today-dated path on an unchanged payload would name
    # an object nobody ever wrote.
    (sid, symbol), _ = two_securities
    blobs = LocalStore(tmp_path)
    body = chart_bytes(date(2026, 9, 4))
    for day in (TODAY, date(2026, 9, 6)):
        run_prices(
            fresh_db,
            client=FakeClient({symbol: body}),
            blobs=blobs,
            today=day,
            securities=[(sid, symbol)],
        )

    paths = [
        row[0]
        for row in fresh_db.execute(
            "select blob_path from ingest_observation where security_id=%s "
            "order by fetched_at",
            (sid,),
        ).fetchall()
    ]
    assert len(paths) == 2
    assert paths[1] == paths[0]          # reused, not re-dated
    assert "2026-09-05" in paths[1]
    assert gzip.decompress(blobs.get(paths[1])) == body
