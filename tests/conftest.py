import os
from pathlib import Path

import psycopg
import pytest

from screener.migrate import apply_migrations

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


@pytest.fixture(scope="session")
def db_url() -> str:
    url = os.environ.get("DATABASE_URL_TEST")
    if not url:
        # Skipping locally is a convenience; skipping in CI is a silent pass of a
        # suite that ran nothing. Fail loudly there instead.
        if os.environ.get("CI"):
            pytest.fail("DATABASE_URL_TEST is not set, but CI is — refusing to skip.")
        pytest.skip("DATABASE_URL_TEST is not set; see docs/plans for setup")
    return url


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
        conn.execute("create schema public")
        yield conn


@pytest.fixture
def fresh_db(empty_db):
    """A connection to an empty public schema with all migrations applied."""
    apply_migrations(empty_db, MIGRATIONS)
    return empty_db
