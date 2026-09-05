"""Giving the read-only role its password, at boot.

The role is created by `migrations/013_playground.sql` with no password, so it
cannot log in. This is what turns it on, from a secret that lives in Infisical
and never in the repository.
"""

import logging

import psycopg
from psycopg import sql

from screener.config import env
from screener.playground.config import BOT_ROLE, ROLE

logger = logging.getLogger(__name__)


# Which environment variable turns each role on. Two, not one, and that is the
# point: whoever holds a password can connect as that role, so a single shared
# one would make the split a matter of which URL the Python picked. Separate
# passwords, held by separate containers, is what makes it a boundary.
PASSWORDS = {
    ROLE: "PLAYGROUND_DB_PASSWORD",
    BOT_ROLE: "PLAYGROUND_BOT_DB_PASSWORD",
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
        conn.execute(
            sql.SQL("alter role {} password {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )
        granted.append(role)
    if granted:
        logger.info("playground: provisioned %s", ", ".join(granted))
    return tuple(granted)
