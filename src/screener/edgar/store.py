"""Writing insider transactions to Postgres. Never opens a socket.

The other half of the split `screener.reddit` and `screener.universe` both
draw: `source` talks to the network and never to the database, this talks to the
database and never to the network. Keeping that visible is what lets either half
be tested without the other being available.
"""

import hashlib
import logging
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal

import psycopg

from screener.edgar.source import Transaction

logger = logging.getLogger(__name__)

SOURCE_CODE = "sec_edgar_form4"

# One `ingest_run` row per day walked, scoped like `screener.reddit`'s
# `wallstreetbets/comment`.
ENDPOINT_PREFIX = "form4"

# How many times a day may fail before it stops being offered. A day EDGAR will
# not serve would otherwise sit at the front of every pass forever, spending the
# whole `days_per_pass` budget on the one date that cannot work.
MAX_ATTEMPTS = 4


def _text(value: object) -> str:
    """One field, as the text it will be hashed as.

    A `Decimal` is rendered from the digits SEC filed rather than through a
    float: `str(Decimal("47.9122"))` round-trips and `str(float("47.9122"))`
    is a different string on a different platform. A date is ISO. Absent is the
    empty string, which is why the separator below has to be a byte that cannot
    appear in a field.
    """
    if value is None:
        return ""
    if isinstance(value, (Decimal, date, datetime)):
        return str(value)
    return str(value)


def content_hash(tx: Transaction) -> bytes:
    """sha256 over everything SEC filed, and nothing this code decided.

    **The two exclusions are the whole design.** `security_id` is our resolution
    of a CIK, so hashing it would make a `universe load` that re-pointed a CIK
    read as SEC restating a trade filed months ago. `fetched_at` is when we
    looked, which changes on every pass by definition.

    What is left is immutable: the archived submission is the same bytes on
    every fetch, so re-walking a day writes nothing at all. That is a stronger
    guarantee than `screener.reddit` has -- there a comment can genuinely be
    edited, and the hash is what tells an edit from a re-fetch. Here a differing
    hash means this parser changed, which is worth seeing in `edited`.
    """
    parts = [
        tx.accession_number, tx.document_type, tx.table_kind, str(tx.seq),
        tx.issuer_cik, tx.issuer_name, _text(tx.issuer_symbol),
        _text(tx.period_of_report), _text(tx.filed_date),
        _text(tx.security_title), _text(tx.transaction_date), tx.transaction_code,
        _text(tx.acquired_disposed), _text(tx.shares), _text(tx.price_per_share),
        _text(tx.shares_owned_after), _text(tx.direct_or_indirect),
        _text(tx.conversion_or_exercise_price), _text(tx.expiration_date),
        _text(tx.underlying_title), _text(tx.underlying_shares),
    ]
    for owner in tx.owners:
        parts.extend(
            [
                owner.cik, owner.name, _text(owner.officer_title),
                str(owner.is_director), str(owner.is_officer),
                str(owner.is_ten_percent_owner), str(owner.is_other),
            ]
        )
    return hashlib.sha256("\x1f".join(parts).encode()).digest()


def source_id(conn: psycopg.Connection, code: str = SOURCE_CODE) -> int:
    with conn.cursor() as cur:
        cur.execute("select id from data_source where code = %s", [code])
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"data_source {code!r} is missing; run migrations")
    return int(row[0])


def universe_ciks(conn: psycopg.Connection) -> dict[str, int]:
    """Every CIK we hold, zero-padded, mapped to its `security.id`.

    Read once per pass and handed to `source.transactions` as data, which is
    what keeps that module free of a database connection.

    **Retired securities are included deliberately.** `is_active` is about index
    membership, and a 2023 filing by a company that left the S&P in 2025 is
    still a fact about a security whose price history we hold.

    Built in Python rather than resolved in SQL because `security.cik` has no
    unique index, and **this is not hypothetical**: on the committed universe it
    fires twice, for `UA`/`UAA` and `NWS`/`NWSA`. A dual-class company is one
    filer with two listed classes, so a Form 4 names one CIK and we hold two
    securities for it. The filing does say which class in `securityTitle`, but
    mapping that text onto a share class is a guess with its own failure mode,
    so this picks the first active row and logs it rather than inventing a
    resolution -- and a warning in the log beats `min(id)` deciding in silence.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select lpad(trim(cik), 10, '0'), id, is_active
            from security
            where cik is not null and trim(cik) <> ''
            order by is_active desc, id
            """
        )
        rows = cur.fetchall()
    out: dict[str, int] = {}
    for cik, security_id, _is_active in rows:
        if cik in out:
            logger.warning(
                "cik %s is held by more than one security; keeping %d", cik, out[cik]
            )
            continue
        out[str(cik)] = int(security_id)
    return out


def walked(
    conn: psycopg.Connection, source: int, *, max_attempts: int = MAX_ATTEMPTS
) -> frozenset[date]:
    """Days not to offer again: finished, or tried enough times.

    **Two different reasons in one set, and that is on purpose.** A day that
    finished is done. A day that has failed `max_attempts` times is not done and
    never will be, and its `ingest_run` rows are the record of that -- but it
    has to stop being offered or it consumes the whole per-pass budget on every
    pass forever. `screener.reddit`'s `pending_gaps` bounds a repair the same
    way and for the same reason.

    `ingest_run` is the frontier rather than a table of its own. It already has
    every column this needs, and it is already granted to all three read-only
    roles -- so "which days has EDGAR done" is answerable on `/playground` with
    no extra grant and no extra migration. `magpie.attempt` is the precedent for
    a log of tries being the control plane.

    **`max(filed_date)` over the stored rows cannot serve.** A day on which no
    issuer in our universe filed produces zero rows, and "walked it, found
    nothing" would then be indistinguishable from "never walked it" -- so that
    day would be re-walked on every pass for ever.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select endpoint,
                   count(*) as tries,
                   count(*) filter (where status = 'ok') as done
            from ingest_run
            where source_id = %s and endpoint like %s
            group by endpoint
            """,
            [source, f"{ENDPOINT_PREFIX}/%"],
        )
        rows = cur.fetchall()
    out: set[date] = set()
    for endpoint, tries, done in rows:
        _, _, stamp = str(endpoint).partition("/")
        try:
            day = date.fromisoformat(stamp)
        except ValueError:
            continue
        if done or tries >= max_attempts:
            out.add(day)
    return frozenset(out)


def save(
    conn: psycopg.Connection,
    source: int,
    securities: Mapping[str, int],
    transactions: list[Transaction],
) -> tuple[int, int]:
    """Upsert a batch. Returns (inserted, edited).

    An unchanged transaction is neither, for `screener.reddit`'s reason: the
    `where ... content_hash is distinct from excluded.content_hash` means a
    re-walk of an archived day writes nothing at all. The counts come from
    `returning`, not from the batch size -- `xmax = 0` is Postgres saying this
    tuple was inserted rather than updated, and a row the `where` excluded
    returns nothing, which is how "unchanged" is counted as neither.

    `security_id` is set on insert and **absent from the update**. The link is
    ours, not SEC's; a filing whose CIK we later resolve differently has not
    changed, and rewriting it here would hide that a reload moved it.
    """
    if not transactions:
        return 0, 0
    now = datetime.now(UTC)
    rows = []
    for tx in transactions:
        security_id = securities.get(tx.issuer_cik)
        if security_id is None:
            # **An expected branch, not a guard.** The index filter matches a
            # filing when *any* of its filers is in the universe, and the filers
            # are the issuer plus every reporting owner -- so a company we hold
            # filing as a ten percent owner of one we do not brings back a
            # filing whose issuer is outside the universe. Measured on
            # 2026-09-11: Corebridge Financial, which we hold, filed against
            # Carlyle Tactical Private Credit Fund, which we do not.
            #
            # The row is dropped rather than stored unlinked, because
            # `security_id` is `not null` and a transaction we cannot attribute
            # to a security we score is not evidence for anything. The wasted
            # fetch is the price of filtering the index instead of every filing.
            logger.info(
                "%s names issuer %s which is not in the universe; skipping",
                tx.accession_number, tx.issuer_cik,
            )
            continue
        rows.append(
            (
                source, security_id, tx.accession_number, tx.document_type,
                tx.table_kind, tx.seq,
                tx.issuer_cik, tx.issuer_name, tx.issuer_symbol,
                tx.period_of_report, tx.filed_date,
                [o.cik for o in tx.owners], [o.name for o in tx.owners],
                [o.officer_title for o in tx.owners if o.officer_title],
                any(o.is_director for o in tx.owners),
                any(o.is_officer for o in tx.owners),
                any(o.is_ten_percent_owner for o in tx.owners),
                any(o.is_other for o in tx.owners),
                tx.security_title, tx.transaction_date, tx.transaction_code,
                tx.acquired_disposed, tx.shares, tx.price_per_share,
                tx.shares_owned_after, tx.direct_or_indirect,
                tx.conversion_or_exercise_price, tx.expiration_date,
                tx.underlying_title, tx.underlying_shares,
                now, content_hash(tx),
            )
        )
    if not rows:
        return 0, 0

    inserted = edited = 0
    with conn.cursor() as cur:
        cur.executemany(
            """
            insert into insider_transaction (
                source_id, security_id, accession_number, document_type,
                table_kind, transaction_seq,
                issuer_cik, issuer_name, issuer_symbol,
                period_of_report, filed_date,
                owner_ciks, owner_names, officer_titles,
                is_director, is_officer, is_ten_percent_owner, is_other,
                security_title, transaction_date, transaction_code,
                acquired_disposed, shares, price_per_share,
                shares_owned_after, direct_or_indirect,
                conversion_or_exercise_price, expiration_date,
                underlying_title, underlying_shares,
                fetched_at, content_hash
            ) values (
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
            )
            on conflict (source_id, accession_number, table_kind, transaction_seq)
            do update set
                document_type = excluded.document_type,
                issuer_cik = excluded.issuer_cik,
                issuer_name = excluded.issuer_name,
                issuer_symbol = excluded.issuer_symbol,
                period_of_report = excluded.period_of_report,
                filed_date = excluded.filed_date,
                owner_ciks = excluded.owner_ciks,
                owner_names = excluded.owner_names,
                officer_titles = excluded.officer_titles,
                is_director = excluded.is_director,
                is_officer = excluded.is_officer,
                is_ten_percent_owner = excluded.is_ten_percent_owner,
                is_other = excluded.is_other,
                security_title = excluded.security_title,
                transaction_date = excluded.transaction_date,
                transaction_code = excluded.transaction_code,
                acquired_disposed = excluded.acquired_disposed,
                shares = excluded.shares,
                price_per_share = excluded.price_per_share,
                shares_owned_after = excluded.shares_owned_after,
                direct_or_indirect = excluded.direct_or_indirect,
                conversion_or_exercise_price = excluded.conversion_or_exercise_price,
                expiration_date = excluded.expiration_date,
                underlying_title = excluded.underlying_title,
                underlying_shares = excluded.underlying_shares,
                fetched_at = excluded.fetched_at,
                content_hash = excluded.content_hash
            where insider_transaction.content_hash is distinct from excluded.content_hash
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
