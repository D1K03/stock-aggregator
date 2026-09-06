"""Giving the read-only role its password, at boot.

The role is created by `migrations/013_playground.sql` with no password, so it
cannot log in. This is what turns it on, from a secret that lives in Infisical
and never in the repository.
"""

import logging
import time

import psycopg
from psycopg import sql

from screener.config import env
from screener.playground.config import BOT_ROLE, MCP_ROLE, ROLE

logger = logging.getLogger(__name__)

# Enough for four test workers to take turns on one shared catalogue row.
_RETRIES = 5


# Which environment variable turns each role on. One per role, and that is the
# point: whoever holds a password can connect as that role, so a single shared
# one would make the split a matter of which URL the Python picked. Separate
# passwords, held by separate containers, is what makes it a boundary.
PASSWORDS = {
    ROLE: "PLAYGROUND_DB_PASSWORD",
    BOT_ROLE: "PLAYGROUND_BOT_DB_PASSWORD",
    MCP_ROLE: "PLAYGROUND_MCP_DB_PASSWORD",
}


def ensure_password(conn: psycopg.Connection) -> tuple[str, ...]:
    """Give each read-only role its password. Idempotent. Returns those set.

    `alter role` takes no query parameters, so the password is composed with
    `psycopg.sql.Literal` — the same sanctioned path `screener.partitions` uses
    for identifiers, and the reason that guardrail allows composition at all.

    An unset variable is not a failure: that role then has no password, cannot
    authenticate, and its caller reports itself switched off. A fresh local
    database is in that state for both, and it should stay quiet about it rather
    than look broken.

    Both are provisioned here, in the api container, because this runs beside
    the migrations under the same advisory lock. The bot never provisions
    anything; it is only handed one of the two URLs.
    """
    granted: list[str] = []
    for role, variable in PASSWORDS.items():
        password = env.optional(variable)
        if password is None:
            logger.info("playground: %s is unset, %s cannot connect", variable, role)
            continue
        _alter(conn, role, password)
        granted.append(role)
    if granted:
        logger.info("playground: provisioned %s", ", ".join(granted))
    return tuple(granted)


def _alter(conn: psycopg.Connection, role: str, password: str) -> None:
    """Set one role's password, retrying a collision with another database.

    `pg_authid` is a *shared* catalog: a role belongs to the cluster, not to a
    database. Two databases in one Postgres provisioning at the same moment
    therefore update the same row, and the loser gets `tuple concurrently
    updated` — an internal error with no SQLSTATE worth branching on, and a
    genuinely transient one. An advisory lock is not the fix, because advisory
    locks are per-database; that was measured rather than assumed.

    In production this never fires: one deployment, one database, and
    `screener.boot` already holds a lock. It is the test suite, whose workers
    each own a database in one cluster, that makes it reachable — and a suite
    that fails one run in ten is worse than one that is slow.
    """
    statement = sql.SQL("alter role {} password {}").format(
        sql.Identifier(role), sql.Literal(password)
    )
    for attempt in range(_RETRIES):
        try:
            conn.execute(statement)
            return
        except psycopg.errors.InternalError_:
            if attempt == _RETRIES - 1:
                raise
            time.sleep(0.05 * (attempt + 1))
