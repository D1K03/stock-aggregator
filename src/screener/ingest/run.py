"""One night of price ingest.

Backfill is not a mode. A security with no rows gets 2020-01-01 from
`windows()`, so cron and a human run the same code and the awkward case — an
outage — exercises the same path as an ordinary night rather than a branch that
only runs once something has already gone wrong.
"""

import gzip
import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import date

import httpx
import psycopg

from screener.blobs import BlobStore, BlobWriteFailed, blob_path
from screener.ingest.load import (
    Change,
    insert_actions,
    insert_settled_bars,
    previous_hash,
    record_observation,
    upsert_unsettled_bars,
)
from screener.ingest.parse import parse
from screener.ingest.window import settling_cutoff, windows

logger = logging.getLogger(__name__)

SOURCE = "yahoo"
ENDPOINT = "chart"


@dataclass
class IngestReport:
    requested: int = 0
    ok: int = 0
    failed: int = 0
    # Two lists rather than one. A settling-window upsert (D5) and a corporate
    # action that differs from what is held (D7) are evidence for two different
    # open decisions — whether `price_daily` becomes bitemporal, and whether
    # `corporate_action` gains a unique constraint — and one combined count
    # answers neither.
    bar_changes: list[Change] = field(default_factory=list)
    action_changes: list[Change] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.ok == 0 and self.requested:
            return "failed"
        return "ok" if self.failed == 0 else "partial"


def source_id(conn: psycopg.Connection) -> int:
    """The `yahoo` data_source row, created on first use."""
    with conn.cursor() as cur:
        # `do nothing` and then read, rather than `do update ... returning`.
        # An upsert here would rewrite a reference row on every single night,
        # which would make `upsert_unsettled_bars` — documented as the only
        # function in the whole ingest path that mutates an existing row —
        # literally untrue.
        cur.execute(
            "insert into data_source (code, name) values (%s, 'Yahoo Finance') "
            "on conflict (code) do nothing",
            (SOURCE,),
        )
        cur.execute("select id from data_source where code = %s", (SOURCE,))
        row = cur.fetchone()
        assert row is not None
        return row[0]


def active_securities(conn: psycopg.Connection) -> list[tuple[int, str]]:
    """Active securities and their current symbol."""
    with conn.cursor() as cur:
        cur.execute(
            """select s.id, ss.symbol
                 from security s
                 join security_symbol ss on ss.security_id = s.id
                where s.is_active and ss.valid_to is null
             order by ss.symbol"""
        )
        return [(row[0], row[1]) for row in cur.fetchall()]


def open_run(conn: psycopg.Connection, requested: int) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """insert into ingest_run
               (source_id, endpoint, started_at, status, securities_requested)
               values (%s, %s, now(), 'running', %s) returning id""",
            (source_id(conn), ENDPOINT, requested),
        )
        row = cur.fetchone()
        assert row is not None
        return row[0]


def close_run(conn: psycopg.Connection, run_id: int, report: IngestReport) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """update ingest_run
                  set finished_at = now(), status = %s, securities_ok = %s
                where id = %s""",
            (report.status, report.ok, run_id),
        )


def run_prices(
    conn: psycopg.Connection,
    *,
    client,
    blobs: BlobStore,
    today: date,
    securities: list[tuple[int, str]],
    run_id: int | None = None,
    delay: float = 0.0,
) -> IngestReport:
    report = IngestReport(requested=len(securities))
    owned_run = run_id is None
    run_id = run_id if run_id is not None else open_run(conn, len(securities))
    starts = windows(conn, [sid for sid, _ in securities], today=today)
    cutoff = settling_cutoff(today)

    for security_id, symbol in securities:
        if delay:
            time.sleep(delay)
        try:
            payload = client.fetch(symbol, starts[security_id], today)
            # A 200 with an empty body is a known Yahoo rate-limit tell (see
            # universe/sources/yahoo.py), and ChartClient.fetch returns it as
            # valid bytes rather than deciding for its caller. We are the
            # caller, and we decide explicitly: an empty payload cannot be
            # parsed into anything meaningful, so it is a failed security, not
            # a security with zero bars. Treating it as success would silently
            # widen the next run's window past a night that never actually
            # answered, and treating it as an exception would end the whole
            # run over what is almost certainly a per-symbol rate limit rather
            # than a systemic outage — so it goes through the same per-security
            # failure path as `None`.
            if payload is None:
                report.failed += 1
                logger.warning("no chart for %s; next run widens its window", symbol)
                continue
            if len(payload) == 0:
                report.failed += 1
                logger.warning(
                    "empty body for %s (likely rate-limited); next run widens its window",
                    symbol,
                )
                continue

            content_hash = hashlib.sha256(payload).digest()
            with conn.cursor() as cur:
                previous = previous_hash(cur, security_id)

            if previous is not None and previous[0] == content_hash:
                # Unchanged payload: no object is written, so the observation
                # reuses the path of the object that *was* written. A
                # today-dated path here would name a blob that does not exist
                # the first time an unchanged payload crosses a date boundary,
                # and `blob_path` is `not null` precisely to stop that.
                is_new = False
                path = previous[1]
            else:
                is_new = True
                path = blob_path(SOURCE, ENDPOINT, today, security_id)
                # Raises on failure, which ends the run: `ingest_observation`
                # asserts blob_path exists, so the row must not be written when
                # the object does not. An R2 error is systemic, not per-object.
                blobs.put(path, gzip.compress(payload))

            bars, actions = parse(payload)
            # One transaction: observation first because it is the FK target, then
            # both fact writes. A run that inserts bars, dies, and leaves the split
            # un-inserted gives a -50% return that looks like real data.
            with conn.transaction():
                with conn.cursor() as cur:
                    observation_id = record_observation(
                        cur,
                        ingest_run_id=run_id,
                        security_id=security_id,
                        content_hash=content_hash,
                        blob_path=path,
                        is_new_payload=is_new,
                        payload_bytes=len(payload),
                    )
                    insert_settled_bars(cur, security_id, observation_id, bars, cutoff)
                    report.bar_changes.extend(
                        upsert_unsettled_bars(cur, security_id, observation_id, bars, cutoff)
                    )
                    report.action_changes.extend(
                        insert_actions(cur, security_id, observation_id, actions)
                    )
        except BlobWriteFailed:
            # D11, and it must stay outside the guard below: a store that
            # cannot be written to is systemic rather than per-object, so
            # continuing would mean ~1,500 doomed securities and observation
            # rows naming objects that were never stored. Re-raised explicitly
            # so a later change to the guard cannot quietly absorb it.
            raise
        except (httpx.HTTPError, httpx.InvalidURL, psycopg.Error) as exc:
            # `InvalidURL` is named separately because it is **not** an
            # `httpx.HTTPError` — it descends straight from `Exception` — so a
            # symbol that will not build a URL escapes `ChartClient._request`,
            # which converts only `HTTPError` to `None`. `chart.py` now escapes
            # the symbol, and this is the belt to that brace.
            #
            # Spec section 7: a transport error or a database error is one
            # security's failure, not the night's. `conn.transaction()` has
            # already rolled this security back whole, and D4 derives the next
            # run's window from `price_daily` rather than from this run, so the
            # gap closes itself tomorrow. Without this boundary a security that
            # fails deterministically would block every alphabetically later
            # one on every subsequent night — `active_securities` orders by
            # symbol — and the window derivation cannot heal what is never
            # reached. Narrow on purpose: a programming error still surfaces.
            report.failed += 1
            logger.warning(
                "%s failed (%s: %s); next run widens its window",
                symbol,
                type(exc).__name__,
                exc,
            )
            continue
        report.ok += 1

    if owned_run:
        close_run(conn, run_id, report)
    return report
