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
