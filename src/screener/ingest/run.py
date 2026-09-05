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

import psycopg

from screener.blobs import BlobStore, blob_path
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
    changes: list[Change] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.ok == 0 and self.requested:
            return "failed"
        return "ok" if self.failed == 0 else "partial"


def source_id(conn: psycopg.Connection) -> int:
    """The `yahoo` data_source row, created on first use."""
    with conn.cursor() as cur:
        cur.execute(
            "insert into data_source (code, name) values (%s, 'Yahoo Finance') "
            "on conflict (code) do update set name = excluded.name returning id",
            (SOURCE,),
        )
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
        path = blob_path(SOURCE, ENDPOINT, today, security_id)

        with conn.cursor() as cur:
            is_new = previous_hash(cur, security_id) != content_hash
        if is_new:
            # Raises on failure, which ends the run: `ingest_observation`
            # asserts blob_path exists, so the row must not be written when the
            # object does not. An R2 error is systemic, not per-object.
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
                report.changes.extend(
                    upsert_unsettled_bars(cur, security_id, observation_id, bars, cutoff)
                )
                report.changes.extend(
                    insert_actions(cur, security_id, observation_id, actions)
                )
        report.ok += 1

    if owned_run:
        close_run(conn, run_id, report)
    return report
