"""Applies .sql files under a migrations directory, in filename order.

Hazard: the applied-migrations ledger keys on `path.stem` (the filename minus
`.sql`), not on file content or a hash. Renaming an already-applied migration
file therefore makes the runner treat it as a brand-new, unapplied migration
and re-run it — silently, and possibly destructively, against a database that
already has it. Never rename a migration file once it has shipped; if a
migration needs fixing after the fact, add a new one instead (per the
project's "never edit an already-applied migration" rule).
"""

from pathlib import Path
from typing import LiteralString, cast

import psycopg

_SCHEMA_MIGRATION = """
create table if not exists schema_migration (
    version    text primary key,
    applied_at timestamptz not null default now()
)
"""


def _ensure_table(conn: psycopg.Connection) -> None:
    conn.execute(_SCHEMA_MIGRATION)


def applied_versions(conn: psycopg.Connection) -> set[str]:
    """Versions already applied to this database."""
    _ensure_table(conn)
    with conn.cursor() as cur:
        cur.execute("select version from schema_migration")
        return {row[0] for row in cur.fetchall()}


def apply_migrations(conn: psycopg.Connection, directory: Path) -> list[str]:
    """Apply every .sql file in `directory` not yet recorded, in filename order.

    Each migration runs in its own transaction: a failure rolls that file back
    and leaves it unrecorded, so a fixed migration can simply be re-run.
    """
    done = applied_versions(conn)
    newly_applied: list[str] = []

    for path in sorted(directory.glob("*.sql")):
        version = path.stem
        if version in done:
            continue
        sql = path.read_text()
        with conn.transaction():
            # psycopg types the query parameter as LiteralString (PEP 675) to
            # catch SQL assembled at runtime from untrusted input. A versioned
            # migration file is trusted repo content, which is the one case the
            # guardrail is meant to allow through.
            conn.execute(cast(LiteralString, sql))
            conn.execute(
                "insert into schema_migration (version) values (%s)", (version,)
            )
        newly_applied.append(version)

    return newly_applied
