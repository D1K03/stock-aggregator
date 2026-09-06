"""One night of fundamentals, against a real database and a fake client."""

import json
from datetime import date
from decimal import Decimal

import pytest

from screener.blobs import BlobWriteFailed
from screener.ingest import run_fundamentals

TODAY = date(2026, 9, 6)


def _payload(revenue="100", period_end="2025-09-30"):
    return json.dumps({
        "timeseries": {"result": [{
            "meta": {"type": ["annualTotalRevenue"]},
            "annualTotalRevenue": [{
                "asOfDate": period_end,
                "reportedValue": {"raw": float(revenue)},
                "currencyCode": "USD",
            }],
        }]}
    }).encode()


class FakeClient:
    def __init__(self, bodies):
        self.bodies = bodies
        self.asked = []

    def fetch(self, symbol):
        self.asked.append(symbol)
        body = self.bodies.get(symbol, b"")
        if isinstance(body, Exception):
            raise body
        return body


class FakeBlobs:
    def __init__(self, fail=False):
        self.written = {}
        self.fail = fail

    def put(self, path, data):
        if self.fail:
            raise BlobWriteFailed(path)
        self.written[path] = data

    def get(self, path):
        # Never exercised by run_fundamentals itself; present only so this
        # fake satisfies the BlobStore protocol for pyright.
        return self.written[path]


@pytest.fixture
def two(fresh_db):
    out = []
    for name, symbol in (("Alpha", "AAA"), ("Beta", "BBB")):
        out.append((
            fresh_db.execute(
                """insert into security
                   (name, mic, currency, country, primary_symbol, first_seen)
                   values (%s, 'XNAS', 'USD', 'US', %s, '2020-01-01')
                   returning id""",
                (name, symbol),
            ).fetchone()[0],
            symbol,
        ))
    return out


def test_a_night_writes_facts_and_an_observation_each(fresh_db, two):
    client = FakeClient({"AAA": _payload("100"), "BBB": _payload("200")})

    report = run_fundamentals(
        fresh_db, client=client, blobs=FakeBlobs(), today=TODAY, securities=two
    )

    assert (report.requested, report.ok, report.failed) == (2, 2, 0)
    assert report.facts_written == 2
    assert fresh_db.execute(
        "select count(*) from ingest_observation"
    ).fetchone()[0] == 2
    assert fresh_db.execute(
        "select endpoint from ingest_run"
    ).fetchone()[0] == "timeseries"


def test_a_second_identical_night_writes_observations_but_no_facts(fresh_db, two):
    bodies = {"AAA": _payload("100"), "BBB": _payload("200")}
    for _ in range(2):
        run_fundamentals(
            fresh_db, client=FakeClient(bodies), blobs=FakeBlobs(),
            today=TODAY, securities=two,
        )

    assert fresh_db.execute(
        "select count(*) from fundamental_fact"
    ).fetchone()[0] == 2
    assert fresh_db.execute(
        "select count(*) from ingest_observation"
    ).fetchone()[0] == 4


def test_every_fact_carries_its_observation_fetched_at(fresh_db, two):
    run_fundamentals(
        fresh_db, client=FakeClient({"AAA": _payload(), "BBB": _payload()}),
        blobs=FakeBlobs(), today=TODAY, securities=two,
    )

    mismatched = fresh_db.execute(
        """select count(*) from fundamental_fact f
             join ingest_observation o on o.id = f.ingest_observation_id
            where f.observed_at <> o.fetched_at"""
    ).fetchone()[0]
    assert mismatched == 0


def test_an_unchanged_payload_writes_no_blob(fresh_db, two):
    bodies = {"AAA": _payload(), "BBB": _payload()}
    blobs = FakeBlobs()
    run_fundamentals(fresh_db, client=FakeClient(bodies), blobs=blobs,
                     today=TODAY, securities=two)
    first = len(blobs.written)

    run_fundamentals(fresh_db, client=FakeClient(bodies), blobs=blobs,
                     today=TODAY, securities=two)

    assert len(blobs.written) == first
    # And the observation still names the object that *was* written.
    paths = fresh_db.execute(
        "select distinct blob_path from ingest_observation"
    ).fetchall()
    assert {p[0] for p in paths} <= set(blobs.written)


def test_one_securitys_failure_does_not_end_the_night(fresh_db, two):
    client = FakeClient({"AAA": _payload(), "BBB": None})

    report = run_fundamentals(
        fresh_db, client=client, blobs=FakeBlobs(), today=TODAY, securities=two
    )

    assert (report.ok, report.failed) == (1, 1)
    assert report.status == "partial"


def test_an_empty_body_is_a_failure_not_a_security_with_no_facts(fresh_db, two):
    client = FakeClient({"AAA": b"", "BBB": _payload()})

    report = run_fundamentals(
        fresh_db, client=client, blobs=FakeBlobs(), today=TODAY, securities=two
    )

    assert report.failed == 1


def test_a_blob_failure_ends_the_run(fresh_db, two):
    # Systemic rather than per-object: continuing would mean observation rows
    # naming objects that were never stored.
    with pytest.raises(BlobWriteFailed):
        run_fundamentals(
            fresh_db, client=FakeClient({"AAA": _payload(), "BBB": _payload()}),
            blobs=FakeBlobs(fail=True), today=TODAY, securities=two,
        )
