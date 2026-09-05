"""Giving the read-only role its password, at boot.

The role is created by `migrations/013_playground.sql` with no password, so it
cannot log in. This is what turns it on, from a secret that lives in Infisical
and never in the repository.
"""

import logging

import psycopg
from psycopg import sql

from screener.config import env
from screener.playground.config import ROLE

logger = logging.getLogger(__name__)


def ensure_password(conn: psycopg.Connection) -> bool:
    """Give the read-only role its password. Idempotent. Returns whether it ran.

    `alter role` takes no query parameters, so the password is composed with
    `psycopg.sql.Literal` — the same sanctioned path `screener.partitions` uses
    for identifiers, and the reason that guardrail allows composition at all.

    False when `PLAYGROUND_DB_PASSWORD` is unset, which is not a failure: the
    role then has no password, cannot authenticate, and the playground is off.
    That is the state a fresh local database is in, and it should stay quiet
    about it rather than look broken.
    """
    password = env.optional("PLAYGROUND_DB_PASSWORD")
    if password is None:
        logger.info("playground: no password set, the read-only role cannot connect")
        return False
    conn.execute(
        sql.SQL("alter role {} password {}").format(
            sql.Identifier(ROLE), sql.Literal(password)
        )
    )
    logger.info("playground: read-only role provisioned")
    return True
