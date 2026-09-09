"""Writing documents to Postgres and payloads to the blob store.

Never opens a socket to a website. That is the split `screener.reddit` and
`screener.universe` both draw, and it is what lets either half be tested without
the other.
"""

import gzip
import hashlib
import logging
import re
from datetime import date
from decimal import Decimal
import psycopg

from screener.blobs import BlobStore, check
from screener.magpie.models import Document, Extracted
from screener.magpie.urls import canonical, host_of

logger = logging.getLogger(__name__)

# Anything outside what `blobs.check` accepts becomes a dash, so a host with an
# internationalised name cannot produce a path the SigV4 signer would have to
# percent-encode.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def content_hash(article: Extracted) -> bytes:
    """sha256 over the headline and the body, and nothing else.

    Deliberately not the response. `screener.reddit` recorded why: hashing a
    whole payload meant Yahoo's hash changed every night and the dedup never
    fired once. An article page carries ad slots, nonces, view counters and a
    rendering timestamp, every one of which moves on each fetch; the headline
    and the body do not, so those are the unit that is stable.
    """
    parts = "\x1f".join([article.title, article.text])
    return hashlib.sha256(parts.encode()).digest()


def blob_path(host: str, day: date, digest: bytes) -> str:
    """Where the raw page is kept.

    `blobs.blob_path` does not fit: it takes a `security_id`, and an article
    mentions zero, one or many securities. Same shape, same validation, keyed on
    what this actually has.
    """
    safe = _UNSAFE.sub("-", host).replace("..", ".").strip(".-") or "unknown"
    return check(f"magpie/{safe}/{day.isoformat()}/{digest.hex()[:16]}.html.gz")


def put_payload(blobs: BlobStore, host: str, day: date, digest: bytes, html: str) -> str:
    """Store the page as fetched, and return its path.

    Kept for the reason `ingest_observation.blob_path` is `not null`: the
    extracted text is an interpretation, and when the interpretation is wrong
    the only way to tell whether the site changed or the parser did is to have
    the bytes that were read.
    """
    path = blob_path(host, day, digest)
    blobs.put(path, gzip.compress(html.encode("utf-8", "replace")))
    return path


def save(
    conn: psycopg.Connection,
    *,
    url: str,
    article: Extracted,
    strategy: str,
    status_code: int,
    blob_path: str,
    digest: bytes,
    requested_by: str | None = None,
) -> Document:
    """Insert or update one document, and say whether anything changed.

    Keyed on the normalised URL, with the hash in the `where` clause rather than
    in a unique constraint — the shape `screener.reddit` uses. A re-scrape that
    found the same article writes nothing at all; a genuine edit updates in
    place; and a syndicated copy at another address is its own row rather than
    being rejected by a constraint it has no business tripping.
    """
    key = canonical(url)
    host = host_of(key)

    with conn.cursor() as cur:
        cur.execute(
            """
            insert into magpie.document (
                url, canonical_url, host, title, author, published,
                language, text, word_count, content_hash, blob_path, strategy,
                status_code, requested_by
            ) values (
                %(url)s, %(canonical_url)s, %(host)s, %(title)s,
                %(author)s, %(published)s, %(language)s, %(text)s, %(word_count)s,
                %(content_hash)s, %(blob_path)s, %(strategy)s, %(status_code)s,
                %(requested_by)s
            )
            on conflict (url) do update set
                canonical_url = excluded.canonical_url,
                title         = excluded.title,
                author        = excluded.author,
                published     = excluded.published,
                language      = excluded.language,
                text          = excluded.text,
                word_count    = excluded.word_count,
                content_hash  = excluded.content_hash,
                blob_path     = excluded.blob_path,
                strategy      = excluded.strategy,
                status_code   = excluded.status_code,
                fetched_at    = now()
            where magpie.document.content_hash is distinct from excluded.content_hash
            returning id, fetched_at, (xmax = 0) as inserted
            """,
            {
                "url": key,
                "canonical_url": article.canonical_url,
                "host": host,
                "title": article.title,
                "author": article.author,
                "published": article.published,
                "language": article.language,
                "text": article.text,
                "word_count": article.word_count,
                "content_hash": digest,
                "blob_path": blob_path,
                "strategy": strategy,
                "status_code": status_code,
                "requested_by": requested_by,
            },
        )
        row = cur.fetchone()

        if row is None:
            # The `where` excluded it: same text as last time, so nothing was
            # written. Read back what is already there rather than reporting a
            # row that was not touched.
            cur.execute(
                "select id, fetched_at from magpie.document where url = %s", (key,)
            )
            existing = cur.fetchone()
            assert existing is not None, "unchanged upsert matched no row"
            document_id, fetched_at, changed = existing[0], existing[1], False
        else:
            document_id, fetched_at, changed = row[0], row[1], True

    return Document(
        id=document_id,
        url=key,
        host=host,
        title=article.title,
        text=article.text,
        word_count=article.word_count,
        strategy=strategy,
        fetched_at=fetched_at,
        author=article.author,
        published=article.published,
        canonical_url=article.canonical_url,
        blob_path=blob_path,
        stored=changed,
    )


def begin_attempt(
    conn: psycopg.Connection, *, url: str, requested_by: str | None = None
) -> int:
    """Open an attempt row and return its id.

    Written before anything is fetched, so a process that dies mid-fetch leaves
    a row saying what it was doing rather than nothing at all — which is how
    skybird tells a capture that failed from one that was never asked for.
    """
    key = canonical(url)
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into magpie.attempt (url, canonical_url, host, requested_by, state)
            values (%s, %s, %s, %s, 'running')
            returning id
            """,
            (url[:2000], key, host_of(key), requested_by),
        )
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


def finish_attempt(
    conn: psycopg.Connection,
    attempt_id: int,
    *,
    state: str,
    reason: str | None = None,
    strategy: str | None = None,
    attempts: tuple[str, ...] = (),
    status_code: int | None = None,
    payload_bytes: int | None = None,
    cost_usd: Decimal = Decimal(0),
    document_id: int | None = None,
    error: str | None = None,
) -> None:
    """Settle an attempt. Never raises — an unsettled row is not worth a failure."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                update magpie.attempt
                   set state = %s, reason = %s, strategy = %s, attempts = %s,
                       status_code = %s, payload_bytes = %s, cost_usd = %s,
                       document_id = %s, error = %s, finished_at = now()
                 where id = %s
                """,
                (
                    state, reason, strategy, list(attempts), status_code,
                    payload_bytes, cost_usd, document_id,
                    (error or "")[:500] or None, attempt_id,
                ),
            )
    except Exception as exc:
        logger.warning("could not settle attempt %s: %s", attempt_id, exc)


def unlocker_used_today(conn: psycopg.Connection) -> int | None:
    """Billed fetches in the last 24 hours. `None` when it cannot be read.

    `None` is not zero and the caller must not treat it as such: unreadable
    means the ladder drops its billed rung and tries the free ones anyway. That
    is a meter that can fail closed without refusing anybody, which the chat
    spend cap cannot do — there, failing closed means silence.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                select count(*) from magpie.attempt
                 where strategy = 'unlocker'
                   and requested_at > now() - interval '24 hours'
                """
            )
            row = cur.fetchone()
        return int(row[0]) if row else 0
    except Exception as exc:
        logger.warning("could not read the unlocker meter: %s", exc)
        return None


def last_refusal(conn: psycopg.Connection, url: str) -> tuple[str, str] | None:
    """The most recent refusal for this address, if there was one.

    So a link that robots.txt forbids is not re-requested, and re-fetched, and
    refused again every time somebody asks about it.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select reason, coalesce(error, '')
              from magpie.attempt
             where canonical_url = %s and state = 'refused'
             order by requested_at desc
             limit 1
            """,
            (canonical(url),),
        )
        row = cur.fetchone()
    return (row[0], row[1]) if row else None


def count_documents(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute("select count(*) from magpie.document")
        row = cur.fetchone()
    return int(row[0]) if row else 0


def recent(
    conn: psycopg.Connection, limit: int = 50, offset: int = 0
) -> list[Document]:
    """A page of documents, newest first. What the dashboard lists.

    Paged rather than capped: this grows on every scrape, and a fixed window is
    a list that quietly stops being the whole story.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, url, host, title, text, word_count, strategy, fetched_at,
                   author, published, canonical_url, blob_path
              from magpie.document
             order by fetched_at desc, id desc
             limit %s offset %s
            """,
            (max(1, min(limit, 200)), max(0, offset)),
        )
        rows = cur.fetchall()
    return [
        Document(
            id=r[0], url=r[1], host=r[2], title=r[3], text=r[4], word_count=r[5],
            strategy=r[6], fetched_at=r[7], author=r[8], published=r[9],
            canonical_url=r[10], blob_path=r[11],
        )
        for r in rows
    ]


def reconcile(conn: psycopg.Connection) -> int:
    """Settle attempts left behind by a process that died mid-fetch.

    Runs once at startup, before anything is fetched, so a row still saying
    'running' at that moment can only belong to a process that is no longer
    there. Marked failed rather than quietly deleted: something really was
    attempted, and a row that vanished would take the evidence with it.

    Skybird's `reconcile`, and for the same reason — the first time this was
    needed here, a container that could not write a blob left two attempts
    reading 'running' for ever, and nothing in the interface could tell them
    from a fetch still in flight.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            update magpie.attempt
               set state = 'failed', reason = 'all_strategies_failed',
                   error = 'the scraper restarted while this was in flight',
                   finished_at = now()
             where state in ('requested', 'running')
            """
        )
        return cur.rowcount


def forget(conn: psycopg.Connection, document_id: int) -> bool:
    """Remove one document. True if there was one to remove.

    The attempts that produced it are kept: `document_id` is `on delete set
    null`, so the record that a page was fetched, by whom and at what cost
    survives the decision to stop keeping its text. Deleting the trail as well
    would mean a link that had been paid for could be re-requested as though it
    were new.

    The stored page is left in the blob store, on `screener.blobs`' terms:
    nothing there prunes, and the path is derived from the host, the date and
    the content hash — so re-scraping the same article writes over the same
    object rather than growing a second one.
    """
    with conn.cursor() as cur:
        cur.execute("delete from magpie.document where id = %s", (document_id,))
        return cur.rowcount == 1
