"""One night of scoring: the run's lifecycle, its reads and every write.

Deliberately unlike ingest, which commits per security. Here a half-scored day
is worse than no day -- tomorrow's crossing diff would compare against it and
invent a crossing for every security that never got scored -- so the whole run
is one transaction (spec D9). Volume makes that free: roughly 9,000 rows a
night against ingest's 2.4 million.
"""

import logging
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import psycopg

from screener.scoring.adjust import Action
from screener.scoring.metrics import months_before

logger = logging.getLogger(__name__)

# A live run scoring D at 02:00 the next morning needs an offset past that
# fetch. The value is stamped on the run and covered by `config_hash`, so
# changing it is visible in the run row rather than only in a deploy.
CUTOFF_OFFSET = timedelta(days=1, hours=6)

# Twelve months for `ret_12m`, plus a month of slack so the nearest bar at or
# before the target is inside the window rather than just outside it.
BAR_WINDOW_MONTHS = 13


def visibility_cutoff(as_of: date, cutoff_offset: timedelta) -> datetime:
    """The instant after which a fact is not visible to this scoring date."""
    return datetime.combine(as_of, datetime.min.time(), tzinfo=timezone.utc) + cutoff_offset


def active_securities(conn: psycopg.Connection) -> list[int]:
    """Every active security's id.

    Ids only, unlike `screener.ingest.active_securities`, which needs the
    current symbol because it is about to fetch one. Scoring never names a
    security to anything outside the database, so importing that function to
    throw half of it away would couple the two cycles for nothing.
    """
    with conn.cursor() as cur:
        cur.execute("select id from security where is_active order by id")
        return [row[0] for row in cur.fetchall()]


def read_bars(
    conn: psycopg.Connection,
    security_ids: Sequence[int],
    *,
    as_of: date,
    cutoff_offset: timedelta,
) -> dict[int, list[tuple[date, Decimal]]]:
    """Visible closes per security, ascending. One query for the whole night."""
    if not security_ids:
        return {}
    out: dict[int, list[tuple[date, Decimal]]] = {}
    with conn.cursor() as cur:
        cur.execute(
            """select security_id, trade_date, close
                 from price_daily
                where security_id = any(%(ids)s)
                  and trade_date > %(start)s
                  and trade_date <= %(as_of)s
                  and observed_at <= %(cutoff)s
             order by security_id, trade_date""",
            {
                "ids": list(security_ids),
                "start": months_before(as_of, BAR_WINDOW_MONTHS),
                "as_of": as_of,
                "cutoff": visibility_cutoff(as_of, cutoff_offset),
            },
        )
        for security_id, trade_date, close in cur.fetchall():
            out.setdefault(security_id, []).append((trade_date, close))
    return out


def read_actions(
    conn: psycopg.Connection,
    security_ids: Sequence[int],
    *,
    as_of: date,
    cutoff_offset: timedelta,
) -> dict[int, list[Action]]:
    """Visible splits and dividends over the same window as the bars."""
    if not security_ids:
        return {}
    out: dict[int, list[Action]] = {}
    with conn.cursor() as cur:
        cur.execute(
            """select security_id, effective_date, action_type, ratio, amount
                 from corporate_action
                where security_id = any(%(ids)s)
                  and effective_date > %(start)s
                  and effective_date <= %(as_of)s
                  and observed_at <= %(cutoff)s
             order by security_id, effective_date""",
            {
                "ids": list(security_ids),
                "start": months_before(as_of, BAR_WINDOW_MONTHS),
                "as_of": as_of,
                "cutoff": visibility_cutoff(as_of, cutoff_offset),
            },
        )
        for security_id, effective_date, action_type, ratio, amount in cur.fetchall():
            out.setdefault(security_id, []).append(
                Action(effective_date, action_type, ratio, amount)
            )
    return out
