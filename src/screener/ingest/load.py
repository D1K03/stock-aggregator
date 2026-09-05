"""Writing bars, actions and the observation that vouches for them.

`upsert_unsettled_bars` is the only function in the whole ingest path that
mutates an existing row. It is a named function rather than an `on conflict`
clause folded into a bulk insert precisely so that it can be found, read and
tested — and so that every value it changes is reported.

That report is the only witness these changes have. The sweep compares Yahoo
against what is already stored, so by the time it runs an in-window change has
already been absorbed and leaves no mismatch to find.
"""

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import psycopg

from screener.ingest.parse import Action, Bar

logger = logging.getLogger(__name__)

BAR_FIELDS = ("open", "high", "low", "close", "volume")


@dataclass(frozen=True)
class Change:
    security_id: int
    on: date
    field: str
    old: Any
    new: Any


def previous_hash(cur: psycopg.Cursor, security_id: int) -> bytes | None:
    """The last content hash seen for this security on the chart endpoint.

    `endpoint` lives on `ingest_run`, not on the observation, so this joins.
    """
    cur.execute(
        """select o.content_hash
             from ingest_observation o
             join ingest_run r on r.id = o.ingest_run_id
            where o.security_id = %s and r.endpoint = 'chart'
         order by o.fetched_at desc
            limit 1""",
        (security_id,),
    )
    row = cur.fetchone()
    return bytes(row[0]) if row else None


def record_observation(
    cur: psycopg.Cursor,
    *,
    ingest_run_id: int,
    security_id: int,
    content_hash: bytes,
    blob_path: str,
    is_new_payload: bool,
    payload_bytes: int,
) -> int:
    """Always written, even when the payload was unchanged (schema D4).

    Dropping it when nothing changed would lose the record of what was known on
    a date, which is the whole point of the trail.
    """
    cur.execute(
        """insert into ingest_observation
           (ingest_run_id, security_id, fetched_at, content_hash, blob_path,
            is_new_payload, payload_bytes)
           values (%s, %s, now(), %s, %s, %s, %s)
           returning id""",
        (
            ingest_run_id,
            security_id,
            content_hash,
            blob_path,
            is_new_payload,
            payload_bytes,
        ),
    )
    row = cur.fetchone()
    assert row is not None
    return row[0]


def insert_settled_bars(
    cur: psycopg.Cursor,
    security_id: int,
    observation_id: int,
    bars: list[Bar],
    cutoff: date,
) -> int:
    """Bars older than the settling window. Insert-if-absent, never modify.

    `on conflict do nothing` is not a mutation: it is how append-only is spelled
    when a re-run legitimately sees rows it already wrote.
    """
    settled = [bar for bar in bars if bar.trade_date < cutoff]
    if not settled:
        return 0
    cur.executemany(
        """insert into price_daily
           (security_id, trade_date, open, high, low, close, volume,
            observed_at, ingest_observation_id)
           values (%s, %s, %s, %s, %s, %s, %s, now(), %s)
           on conflict (security_id, trade_date) do nothing""",
        [
            (
                security_id,
                bar.trade_date,
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                observation_id,
            )
            for bar in settled
        ],
    )
    return len(settled)


def upsert_unsettled_bars(
    cur: psycopg.Cursor,
    security_id: int,
    observation_id: int,
    bars: list[Bar],
    cutoff: date,
) -> list[Change]:
    """Bars inside the settling window. **The one mutating path.**

    Yahoo revises the most recent session — volume in particular, as
    consolidated tape arrives — so a bar is not final the moment it appears.
    Every changed value is returned and logged, because nothing else can see it.
    """
    unsettled = [bar for bar in bars if bar.trade_date >= cutoff]
    changes: list[Change] = []
    for bar in unsettled:
        cur.execute(
            """select open, high, low, close, volume
                 from price_daily
                where security_id = %s and trade_date = %s""",
            (security_id, bar.trade_date),
        )
        existing = cur.fetchone()
        if existing is not None:
            for field, old in zip(BAR_FIELDS, existing):
                new = getattr(bar, field)
                if old != new:
                    change = Change(security_id, bar.trade_date, field, old, new)
                    changes.append(change)
                    logger.info(
                        "settling-window change: security=%s %s %s %s -> %s",
                        security_id,
                        bar.trade_date,
                        field,
                        old,
                        new,
                    )
        cur.execute(
            """insert into price_daily
               (security_id, trade_date, open, high, low, close, volume,
                observed_at, ingest_observation_id)
               values (%s, %s, %s, %s, %s, %s, %s, now(), %s)
               on conflict (security_id, trade_date) do update set
                 open = excluded.open,
                 high = excluded.high,
                 low = excluded.low,
                 close = excluded.close,
                 volume = excluded.volume,
                 observed_at = excluded.observed_at,
                 ingest_observation_id = excluded.ingest_observation_id""",
            (
                security_id,
                bar.trade_date,
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                observation_id,
            ),
        )
    return changes


def insert_actions(
    cur: psycopg.Cursor,
    security_id: int,
    observation_id: int,
    actions: list[Action],
) -> list[Change]:
    """Insert-if-absent on (security, date, type); log a difference, never write it.

    `corporate_action` has no unique constraint, and the events block returns
    everything inside the requested window — so without this a dividend is
    re-inserted on each of the next several nights, and duplicated dividends
    corrupt exactly the adjustment schema D6 computes at scoring time.

    A hard constraint would also stop the duplicates, but it would permanently
    block a provider revising an amount. Detect, log, decide with evidence.
    """
    changes: list[Change] = []
    for action in actions:
        cur.execute(
            """select ratio, amount
                 from corporate_action
                where security_id = %s and effective_date = %s and action_type = %s""",
            (security_id, action.effective_date, action.action_type),
        )
        existing = cur.fetchone()
        if existing is not None:
            for field, old, new in (
                ("ratio", existing[0], action.ratio),
                ("amount", existing[1], action.amount),
            ):
                if old != new:
                    change = Change(security_id, action.effective_date, field, old, new)
                    changes.append(change)
                    logger.warning(
                        "corporate action differs, not written: security=%s %s %s %s -> %s",
                        security_id,
                        action.effective_date,
                        field,
                        old,
                        new,
                    )
            continue
        cur.execute(
            """insert into corporate_action
               (security_id, effective_date, action_type, ratio, amount,
                currency, observed_at, ingest_observation_id)
               values (%s, %s, %s, %s, %s, %s, now(), %s)""",
            (
                security_id,
                action.effective_date,
                action.action_type,
                action.ratio,
                action.amount,
                "USD",
                observation_id,
            ),
        )
    return changes
