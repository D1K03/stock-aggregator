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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg

from screener.ingest.facts import Fact
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


def previous_hash(
    cur: psycopg.Cursor, security_id: int, endpoint: str
) -> tuple[bytes, str] | None:
    """The last `(content_hash, blob_path)` seen for this security on `endpoint`.

    The path comes back with the hash because the two are only meaningful
    together: when tonight's hash matches, no object is written, so the
    observation must name the object that *was* written rather than a
    today-dated path with nothing behind it. `blob_path` is `not null`
    precisely so that "every score traces back to the stored response" is a
    claim the database enforces.

    `endpoint` lives on `ingest_run`, not on the observation, so this joins.
    Parameterised rather than hard-coded to `'chart'` because fundamentals
    shares this table under a different endpoint — without the parameter
    tonight's fundamentals hash would compare against yesterday's *price*
    hash, never match, and write a blob every night.
    """
    cur.execute(
        """select o.content_hash, o.blob_path
             from ingest_observation o
             join ingest_run r on r.id = o.ingest_run_id
            where o.security_id = %s and r.endpoint = %s
         order by o.fetched_at desc
            limit 1""",
        (security_id, endpoint),
    )
    row = cur.fetchone()
    return (bytes(row[0]), row[1]) if row else None


def record_observation(
    cur: psycopg.Cursor,
    *,
    ingest_run_id: int,
    security_id: int,
    content_hash: bytes,
    blob_path: str,
    is_new_payload: bool,
    payload_bytes: int,
) -> tuple[int, datetime]:
    """Always written, even when the payload was unchanged (schema D4).

    Dropping it when nothing changed would lose the record of what was known on
    a date, which is the whole point of the trail.

    Returns `fetched_at` alongside the id because the fundamentals path stamps
    every fact from one payload with it: `observed_at` has to be one value per
    observation, not a clock read per row, or two facts from one response sort
    non-deterministically against each other and a restatement chain can point
    forwards in time.
    """
    cur.execute(
        """insert into ingest_observation
           (ingest_run_id, security_id, fetched_at, content_hash, blob_path,
            is_new_payload, payload_bytes)
           values (%s, %s, now(), %s, %s, %s, %s)
           returning id, fetched_at""",
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
    return row[0], row[1]


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


@dataclass(frozen=True)
class Held:
    """The latest fact held for one (metric, period_end, period_type)."""

    fact_id: int
    value: Decimal


def metric_ids(cur: psycopg.Cursor) -> dict[str, int]:
    """`metric.code` -> id, for the input metrics only.

    Filtered on `is_input` rather than returning everything, so a fact naming a
    *scored* metric — which would mean a ratio had been stored, against D3 —
    cannot be written by accident.
    """
    cur.execute("select code, id from metric where is_input")
    return {row[0]: row[1] for row in cur.fetchall()}


def latest_values(
    cur: psycopg.Cursor, security_id: int
) -> dict[tuple[str, date, str], Held]:
    """What we already hold for this security, latest observation per period.

    Keyed by `(metric_code, period_end, period_type)` — `period_type` is in the
    key because a fiscal Q4 ends when its fiscal year does, so without it a
    year and its own last quarter collapse into one entry and every night would
    see one of them as changed.
    """
    cur.execute(
        """select distinct on (f.metric_id, f.period_end, f.period_type)
                  m.code, f.period_end, f.period_type, f.id, f.value
             from fundamental_fact f
             join metric m on m.id = f.metric_id
            where f.security_id = %s
         order by f.metric_id, f.period_end, f.period_type, f.observed_at desc""",
        (security_id,),
    )
    return {
        (code, period_end, period_type): Held(fact_id, value)
        for code, period_end, period_type, fact_id, value in cur.fetchall()
    }


def insert_facts(
    cur: psycopg.Cursor,
    security_id: int,
    observation_id: int,
    observed_at: datetime,
    facts: Sequence[Fact],
    held: Mapping[tuple[str, date, str], Held],
    ids: Mapping[str, int],
) -> int:
    """Insert only the facts whose value differs from what is held.

    `observed_at` is the caller's — the observation's `fetched_at` — and is the
    same for every fact from one payload. Never `now()` per row.

    A fact whose value matches writes nothing at all: not a row, and (upstream)
    not a blob. The observation is what records that we looked.
    """
    rows = []
    for fact in facts:
        metric_id = ids.get(fact.metric_code)
        if metric_id is None:
            # The parser only emits codes from SERIES, so this is unreachable
            # from a real payload. Skipping rather than raising keeps a seed
            # that has drifted from costing a whole security.
            continue
        key = (fact.metric_code, fact.period_end, fact.period_type)
        previous = held.get(key)
        if previous is not None and previous.value == fact.value:
            continue
        rows.append(
            (
                security_id, metric_id, fact.period_end, fact.period_type,
                fact.value, fact.currency, observed_at, observation_id,
                previous.fact_id if previous else None,
            )
        )
    if not rows:
        return 0
    cur.executemany(
        """insert into fundamental_fact
           (security_id, metric_id, period_end, period_type, value, currency,
            observed_at, ingest_observation_id, restates_id)
           values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
           on conflict (security_id, metric_id, period_end, period_type,
                        observed_at) do nothing""",
        rows,
    )
    return len(rows)


@dataclass(frozen=True)
class HeldFact:
    metric_code: str
    period_end: date
    period_type: str
    value: Decimal
    currency: str | None
    observed_at: datetime


def read_facts(
    conn: psycopg.Connection,
    security_ids: Sequence[int],
    *,
    as_of: date,
    cutoff_offset: timedelta,
) -> dict[int, list[HeldFact]]:
    """What each security's facts were, as known on `as_of`.

    The bitemporal read, and the only thing that can show the writes were
    correct: rows existing proves nothing, because a test asserting the fields
    it just passed in is a tautology.

    **`period_type` is in the `distinct on` and it has to be.** A fiscal Q4
    ends on the same date as its fiscal year -- AAPL reports both at 2025-09-30,
    four-fold apart -- so the read documented in the schema spec,
    `distinct on (security_id, metric_id, period_end)`, returns whichever was
    inserted later and silently discards the other. For every company, every
    year. Migration 021 puts `period_type` into the index too, in the order this
    `order by` uses, so the `distinct on` is satisfied without a sort.

    `cutoff` comes from `screener.scoring.visibility_cutoff` rather than being
    recomputed here, so prices and fundamentals answer to one definition of
    what a scoring date may see.
    """
    if not security_ids:
        return {}
    # Imported here rather than at module scope, and it has to stay here:
    # `screener.scoring.run` imports this function at *its* module scope, so a
    # top-level import in this direction would make the two packages a cycle
    # that fails on whichever is imported first.
    from screener.scoring import visibility_cutoff

    out: dict[int, list[HeldFact]] = {}
    with conn.cursor() as cur:
        cur.execute(
            """select distinct on (f.security_id, f.metric_id, f.period_end,
                                   f.period_type)
                      f.security_id, m.code, f.period_end, f.period_type,
                      f.value, f.currency, f.observed_at
                 from fundamental_fact f
                 join metric m on m.id = f.metric_id
                where f.security_id = any(%(ids)s)
                  and f.observed_at <= %(cutoff)s
             order by f.security_id, f.metric_id, f.period_end, f.period_type,
                      f.observed_at desc""",
            {
                "ids": list(security_ids),
                "cutoff": visibility_cutoff(as_of, cutoff_offset),
            },
        )
        for security_id, code, period_end, period_type, value, currency, observed_at in cur.fetchall():
            out.setdefault(security_id, []).append(
                HeldFact(code, period_end, period_type, value, currency, observed_at)
            )
    return out
