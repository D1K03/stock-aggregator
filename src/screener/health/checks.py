"""The checks behind /ready. None of them raise."""

import psycopg

from screener.config import settings

CONNECT_TIMEOUT = 3


def database() -> tuple[str, int | None]:
    """Whether Postgres answers, and how many migrations it has applied.

    Returns a reason rather than raising, and the reason is an exception *type
    name*, never its message: psycopg embeds the host and usually the username
    in connection errors, and /ready is reachable by anyone who gets past
    Access.

    A fresh connection per probe, never a cached one. The question is "can a
    connection be made now"; a pooled handle that an idle timeout killed minutes
    ago answers "one could be made earlier", which is worse than no answer at
    all. One connection every thirty seconds is not worth a pool.
    """
    try:
        url = settings().database_url
    except RuntimeError:
        return "unconfigured", None

    try:
        with psycopg.connect(url, connect_timeout=CONNECT_TIMEOUT) as conn:
            with conn.cursor() as cur:
                cur.execute("select count(*) from schema_migration")
                row = cur.fetchone()
                applied = int(row[0]) if row else 0
        return "ok", applied
    except psycopg.errors.UndefinedTable:
        # Reachable, but nothing has been applied yet. Distinguishing this from
        # "cannot connect" is the difference between waiting for a deploy to
        # finish and waking someone up.
        return "no schema", 0
    except Exception as exc:
        return type(exc).__name__, None
