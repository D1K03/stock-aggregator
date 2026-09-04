"""The startup sequence and its command line."""

import argparse
import logging
import pathlib
import sys
from datetime import date

import psycopg

from screener.config import settings
from screener.health import serve
from screener.migrate import apply_migrations
from screener.partitions import ensure_partitions
from screener.secrets import SecretsError, load_into_environ

logger = logging.getLogger(__name__)

MIGRATIONS = pathlib.Path("migrations")

# Any 64-bit constant will do; it only has to be unique within this database.
# Advisory locks are scoped per-database rather than per-server, so this cannot
# collide with another application even on a shared instance.
MIGRATION_LOCK_ID = 8_119_002

# How far ahead to pre-create partitions. A year of headroom means a missing
# partition never becomes a failed insert at midnight on 1 January.
PARTITION_HEADROOM_YEARS = 1


def prepare_database() -> None:
    """Apply migrations and pre-create partitions, under an advisory lock.

    The lock matters because `apply_migrations` reads the applied set and then
    runs DDL, with nothing in between to stop a second process doing the same.
    Two containers starting at once — a rolling deploy, or a restart racing a
    manual `compose up` — would both see version N as unapplied and both run
    its `CREATE TABLE`; the loser dies on `DuplicateTable`, which reads like a
    broken migration rather than a race. It costs one round trip on a path that
    already talks to the database, which is cheap next to a deploy that fails
    with a message pointing at the wrong thing.

    The lock is session-scoped, so a container killed mid-migration releases it
    when its socket dies rather than wedging every future deploy.
    """
    url = settings().database_url
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("select pg_advisory_lock(%s)", (MIGRATION_LOCK_ID,))
        try:
            applied = apply_migrations(conn, MIGRATIONS)
            logger.info(
                "migrations: %s", ", ".join(applied) if applied else "already current"
            )
            created = ensure_partitions(
                conn, through_year=date.today().year + PARTITION_HEADROOM_YEARS
            )
            logger.info(
                "partitions: %s", ", ".join(created) if created else "already current"
            )
        finally:
            conn.execute("select pg_advisory_unlock(%s)", (MIGRATION_LOCK_ID,))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="screener.boot", description=__doc__)
    parser.add_argument(
        "command",
        nargs="?",
        default="serve",
        choices=("serve", "migrate", "selftest"),
        help=(
            "serve: prepare the database then run the health service (default). "
            "migrate: prepare the database and exit. "
            "selftest: check every configured integration and exit."
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    try:
        load_into_environ()
    except SecretsError as exc:
        # Fatal on purpose. Starting with half a configuration means failing
        # later, somewhere less obvious, against a database that may be the
        # wrong one.
        logger.error("%s", exc)
        return 1

    if args.command == "selftest":
        from screener.boot.selftest import run

        return 0 if run() else 1

    try:
        prepare_database()
    except Exception as exc:
        logger.error("database preparation failed, refusing to start: %s", exc)
        return 1

    if args.command == "migrate":
        return 0

    # Called directly rather than exec'd. The sibling project execs because its
    # server is a different program; ours is in this interpreter, and keeping it
    # here means the SIGTERM handler in `serve()` is the one that runs.
    serve()
    return 0
