import os
import time
from pathlib import Path

import psycopg
import pytest

from screener.migrate import apply_migrations

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


@pytest.fixture(scope="session")
def db_url(request) -> str:
    """The throwaway database this worker owns.

    One per xdist worker, and it has to be: every test drops and recreates
    `public`, so two workers sharing a database would delete each other's tables
    mid-test. The name carries the worker id, and running without xdist gets the
    plain URL exactly as before.

    Why parallelism at all, when the suite is already fast per test: it is not.
    Applying eighteen migrations costs about 320ms and every database test pays
    it, which is two thirds of the wall time and cannot be avoided — the app
    opens its own connections everywhere, so there is no transaction to roll
    back, and `create database ... template` measured *slower* than migrating.
    The work is irreducible; running it on four cores is not.
    """
    url = os.environ.get("DATABASE_URL_TEST")
    if not url:
        # Skipping locally is a convenience; skipping in CI is a silent pass of a
        # suite that ran nothing. Fail loudly there instead.
        if os.environ.get("CI"):
            pytest.fail("DATABASE_URL_TEST is not set, but CI is — refusing to skip.")
        pytest.skip("DATABASE_URL_TEST is not set; see docs/plans for setup")

    worker = getattr(request.config, "workerinput", {}).get("workerid")
    if worker is None:
        return url

    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    base = str(conninfo_to_dict(url).get("dbname") or "screener_test")
    mine = f"{base}_{worker}"
    # `sql.Identifier`, not an f-string: psycopg types a query as LiteralString
    # so that SQL assembled at runtime is rejected, and composition is the one
    # sanctioned way through — the same path `screener.partitions` uses.
    #
    # From `postgres`, and autocommit, because you cannot create a database from
    # inside the one you are dropping and `create database` cannot run in a
    # transaction.
    with psycopg.connect(make_conninfo(url, dbname="postgres"), autocommit=True) as conn:
        name = sql.Identifier(mine)
        conn.execute(sql.SQL("drop database if exists {}").format(name))
        conn.execute(sql.SQL("create database {}").format(name))
    return make_conninfo(url, dbname=mine)


@pytest.fixture
def empty_db(db_url):
    """A connection to an empty public schema. No migrations applied."""
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("drop schema if exists public cascade")
        # Migration 010 puts sign-in state in its own schema, which `drop schema
        # public` does not reach. Without this the auth tables survive between
        # tests and the migration runner fails re-creating them.
        conn.execute("drop schema if exists auth cascade")
        conn.execute("drop schema if exists audit cascade")
        conn.execute("drop schema if exists skybird cascade")
        conn.execute("drop schema if exists mcp cascade")
        conn.execute("create schema public")
        yield conn


@pytest.fixture
def fresh_db(empty_db):
    """A connection to an empty public schema with all migrations applied.

    Retried on `InternalError_`, which here means one thing: two workers
    creating the same cluster-wide role in the same instant. Roles are not
    per-database, so the first migration of a run is the one moment several
    databases write the same `pg_authid` row, and Postgres fails the loser with
    "tuple concurrently updated". Everything after that is a no-op, because the
    role migrations only write their settings when absent.

    An advisory lock is not the fix, because advisory locks are scoped per
    database, which is worth stating since it is the obvious thing to reach for.
    """
    for attempt in range(4):
        try:
            apply_migrations(empty_db, MIGRATIONS)
            return empty_db
        except psycopg.errors.InternalError_:
            if attempt == 3:
                raise
            time.sleep(0.1 * (attempt + 1))
    return empty_db


@pytest.fixture
def an_observation(fresh_db):
    """Make an ingest_observation for a security, and return its id.

    `price_daily.ingest_observation_id` and `corporate_action.ingest_observation_id`
    are both not-null foreign keys, so nothing can be inserted without one.
    A fixture returning a callable rather than a plain function, because a test
    module cannot import from conftest without relying on pytest's import mode.
    """

    def make(security_id: int) -> int:
        source_id = fresh_db.execute(
            "insert into data_source (code, name) values ('yahoo', 'Yahoo') "
            "on conflict (code) do update set name = excluded.name returning id"
        ).fetchone()[0]
        run_id = fresh_db.execute(
            "insert into ingest_run (source_id, endpoint, started_at, status) "
            "values (%s, 'chart', now(), 'running') returning id",
            (source_id,),
        ).fetchone()[0]
        return fresh_db.execute(
            """insert into ingest_observation
               (ingest_run_id, security_id, fetched_at, content_hash, blob_path,
                is_new_payload, payload_bytes)
               values (%s, %s, now(), %s, 'yahoo/chart/2026-09-05/1.json.gz', true, 1)
               returning id""",
            (run_id, security_id, b"\x00" * 32),
        ).fetchone()[0]

    return make


@pytest.fixture
def ingest_ctx(fresh_db, an_observation):
    """One security and an observation, as (security_id, observation_id)."""
    security_id = fresh_db.execute(
        """insert into security
           (name, mic, currency, country, primary_symbol, first_seen)
           values ('Test', 'XNAS', 'USD', 'US', 'AAA', '2020-01-01') returning id"""
    ).fetchone()[0]
    return security_id, an_observation(security_id)


@pytest.fixture
def two_securities(fresh_db):
    """Two securities as (id, symbol) pairs."""
    out = []
    for name, symbol in (("Alpha", "AAA"), ("Beta", "BBB")):
        out.append(
            (
                fresh_db.execute(
                    """insert into security
                       (name, mic, currency, country, primary_symbol, first_seen)
                       values (%s, 'XNAS', 'USD', 'US', %s, '2020-01-01') returning id""",
                    (name, symbol),
                ).fetchone()[0],
                symbol,
            )
        )
    return out[0], out[1]


def _chart_bytes(day, close="100", volume=10, split=None):
    """A minimal chart response. Used by the run and sweep tests."""
    import json
    from datetime import datetime, timezone

    stamp = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
    body = {
        "chart": {
            "result": [
                {
                    "meta": {"currency": "USD"},
                    "timestamp": [stamp],
                    "indicators": {
                        "quote": [
                            {
                                "open": [float(close)],
                                "high": [float(close)],
                                "low": [float(close)],
                                "close": [float(close)],
                                "volume": [volume],
                            }
                        ]
                    },
                }
            ]
        }
    }
    if split:
        body["chart"]["result"][0]["events"] = {
            "splits": {str(stamp): {"date": stamp, "numerator": 2, "denominator": 1}}
        }
    return json.dumps(body).encode()


@pytest.fixture
def chart_bytes():
    """A test module cannot import a plain function from conftest, so the
    module-level helper (`_chart_bytes`) is handed out through a fixture —
    the same shape as `an_observation` above."""
    return _chart_bytes


class _FakeClient:
    """A ChartClient-shaped stub, keyed by symbol so one can fail."""

    def __init__(self, bodies):
        self.bodies = bodies
        self.asked = []

    def fetch(self, symbol, start, end):
        self.asked.append((symbol, start, end))
        return self.bodies.get(symbol)


@pytest.fixture
def FakeClient():
    """Hands out the stub class itself, for the same reason as `chart_bytes`:
    a test module cannot import a plain name from conftest."""
    return _FakeClient
