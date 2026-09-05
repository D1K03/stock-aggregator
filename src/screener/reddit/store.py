"""Writing social items to Postgres. Never opens a socket.

The other half of the split `screener.universe` draws: `source` talks to the
network and never to the database, this talks to the database and never to the
network. Keeping that visible is what lets either half be tested without the
other being available.
"""

import hashlib
import logging
from datetime import UTC, datetime

import psycopg

from screener.reddit.source import Item

logger = logging.getLogger(__name__)

SOURCE_CODE = "arctic_shift"


def content_hash(item: Item) -> bytes:
    """sha256 over the fields that carry meaning.

    Deliberately not the whole item. `score` moves every time anyone votes, so
    hashing it would mean every re-fetch looked like an edit and the dedup this
    exists for would never fire — which is exactly what was measured happening
    to Yahoo's bundled `quoteSummary`, where eight fields move on a trading day
    and the hash therefore never matches. Here the unit that is stable is the
    text, so the text is what is hashed.
    """
    parts = "\x1f".join(
        [item.external_id, item.title or "", item.body, item.author or ""]
    )
    return hashlib.sha256(parts.encode()).digest()


def source_id(conn: psycopg.Connection, code: str = SOURCE_CODE) -> int:
    with conn.cursor() as cur:
        cur.execute("select id from data_source where code = %s", [code])
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"data_source {code!r} is missing; run migrations")
    return int(row[0])


def latest_seen(
    conn: psycopg.Connection, source: int, subreddit: str, kind: str
) -> datetime | None:
    """The newest item already stored, or None when there is nothing yet.

    This is what decides backfill from incremental, so it is asked per subreddit
    and per kind rather than once: adding a subreddit to the list should backfill
    that one without re-walking the others.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select max(created_utc) from social_item
            where source_id = %s and subreddit = %s and kind = %s
            """,
            [source, subreddit, kind],
        )
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def earliest_seen(
    conn: psycopg.Connection, source: int, subreddit: str, kind: str
) -> datetime | None:
    """The oldest item already stored, or None when there is nothing yet.

    The counterpart to `latest_seen`, and the reason a backfill can be finished
    across several passes. The walk goes backwards from now, so an interrupted
    one leaves the newest slice stored and the older end missing; resuming from
    the newest item alone would never come back for it.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select min(created_utc) from social_item
            where source_id = %s and subreddit = %s and kind = %s
            """,
            [source, subreddit, kind],
        )
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def save(conn: psycopg.Connection, source: int, items: list[Item]) -> tuple[int, int]:
    """Upsert a batch. Returns (inserted, edited).

    An unchanged item is neither: `where social_item.content_hash is distinct
    from excluded.content_hash` means a re-fetch that found the same text writes
    nothing at all, which is the whole point of hashing per item. A changed hash
    is a real edit and updates the row in place — the trail of what was known
    when lives on `fetched_at`, not on a second copy of the same comment.

    The counts come from `returning`, not from the batch size. An earlier
    version counted the rows that existed afterwards, which is every row it was
    given every time, so a pass that changed nothing still reported a full batch
    and the logs could not tell a working dedup from a broken one. `xmax = 0` is
    Postgres saying this tuple was inserted rather than updated; a row excluded
    by the `where` returns nothing at all, which is how "unchanged" is counted.
    """
    if not items:
        return 0, 0
    now = datetime.now(UTC)
    rows = [
        (
            source, i.kind, i.external_id, i.subreddit, i.parent_id, i.author,
            i.created_utc, now, i.score, i.title, i.body, i.permalink,
            content_hash(i),
        )
        for i in items
    ]
    inserted = edited = 0
    with conn.cursor() as cur:
        cur.executemany(
            """
            insert into social_item (
                source_id, kind, external_id, subreddit, parent_id, author,
                created_utc, fetched_at, score, title, body, permalink, content_hash
            ) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            on conflict (source_id, external_id) do update set
                score = excluded.score,
                title = excluded.title,
                body = excluded.body,
                fetched_at = excluded.fetched_at,
                content_hash = excluded.content_hash
            where social_item.content_hash is distinct from excluded.content_hash
            returning (xmax = 0) as inserted
            """,
            rows,
            returning=True,
        )
        while True:
            row = cur.fetchone()
            if row is not None:
                if row[0]:
                    inserted += 1
                else:
                    edited += 1
            if not cur.nextset():
                break
    return inserted, edited


def start_run(conn: psycopg.Connection, source: int, endpoint: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into ingest_run (source_id, endpoint, started_at, status)
            values (%s, %s, now(), 'running') returning id
            """,
            [source, endpoint],
        )
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


def finish_run(
    conn: psycopg.Connection, run_id: int, status: str, error: str | None = None
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "update ingest_run set finished_at = now(), status = %s, error = %s where id = %s",
            [status, error[:500] if error else None, run_id],
        )
