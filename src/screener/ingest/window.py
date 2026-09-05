"""How far back to ask, per security.

Two windows, and they must not be one number:

- the **fetch** window is how much to request, and widens to close a gap after
  an outage;
- the **settling** window is how far back an upsert is permitted, and never
  widens.

Sharing a number would let a catch-up run silently rewrite bars that had already
settled.

Derived per security from `price_daily` rather than from the last successful
`ingest_run`. Failures are per security but a run's status is per run, so a
run-level window never goes back for what a `partial` night missed. Per security
it self-heals, and a security added by the quarterly universe refresh has no rows
at all, so it backfills with no special case.
"""

from datetime import date, timedelta

import psycopg

# Migration 008 pre-creates yearly partitions from here, for exactly this.
BACKFILL_START = date(2020, 1, 1)

# Calendar days, not trading days, so no market calendar is needed. Seven covers
# a normal trading week across a weekend. Yahoo appears to revise only the most
# recent session, so this is generous on purpose; the upsert log is what would
# justify shortening it.
SETTLING_DAYS = 7


def settling_cutoff(today: date) -> date:
    return today - timedelta(days=SETTLING_DAYS)


def windows(
    conn: psycopg.Connection, security_ids: list[int], *, today: date
) -> dict[int, date]:
    """The first date to request, per security. Every id gets an answer."""
    if not security_ids:
        return {}
    cutoff = settling_cutoff(today)
    held: dict[int, date] = {}
    with conn.cursor() as cur:
        cur.execute(
            """select security_id, max(trade_date)
                 from price_daily
                where security_id = any(%s)
             group by security_id""",
            (security_ids,),
        )
        for security_id, latest in cur.fetchall():
            held[security_id] = latest
    return {
        security_id: (
            # `min`, not the `max` D4 of the spec writes. Taken literally the
            # spec's formula is wrong at exactly the ordinary case: a security
            # held to yesterday would start at *today*, the settling window
            # would never be re-requested, and D5's upsert — the one path
            # allowed to absorb Yahoo's revisions to recent sessions — would
            # have nothing to work on. The window has to reach back to the
            # earlier of "where we stopped" and "the settling cutoff". The
            # spec carries an erratum note on D4 saying the same.
            min(held[security_id] + timedelta(days=1), cutoff)
            if security_id in held
            else BACKFILL_START
        )
        for security_id in security_ids
    }
