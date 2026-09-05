"""Reading the trail back."""

from decimal import Decimal
from typing import Any

import psycopg
from psycopg import sql

from screener.audit.models import KINDS, ActorSpend, Event, Spend

# The interface pages in fifties. Fixed rather than caller-supplied: a page
# size in a query string is a way to ask for the whole table at once.
PAGE_SIZE = 50

CONNECT_TIMEOUT = 3

# Composed rather than interpolated: psycopg types a query as LiteralString so
# that SQL built at runtime is rejected outright, and a filter clause assembled
# from user input is exactly what that guardrail is for.
_COLUMNS = sql.SQL(
    "id, occurred_at, kind, operation, actor, actor_kind, outcome, model, "
    "prompt_tokens, completion_tokens, cost_usd, duration_ms, detail"
)


def _row(values: tuple[Any, ...]) -> Event:
    return Event(
        id=values[0],
        occurred_at=values[1],
        kind=values[2],
        operation=values[3],
        actor=values[4],
        actor_kind=values[5],
        outcome=values[6],
        model=values[7],
        prompt_tokens=values[8],
        completion_tokens=values[9],
        cost_usd=values[10],
        duration_ms=values[11],
        detail=values[12] or {},
    )


def page(
    conn: psycopg.Connection,
    *,
    kind: str | None = None,
    operation: str | None = None,
    offset: int = 0,
) -> tuple[list[Event], int]:
    """One page of events, newest first, and how many match in total.

    The count comes back alongside so the interface can say "page 3 of 9"
    rather than only discovering the end by walking off it.

    An unknown `kind` is ignored rather than returning nothing. A filter value
    that is not in the enum can only come from a hand-edited URL, and an empty
    table is a worse answer than an unfiltered one.
    """
    conditions: list[sql.Composable] = []
    params: list[Any] = []
    if kind in KINDS:
        conditions.append(sql.SQL("kind = %s"))
        params.append(kind)
    if operation:
        conditions.append(sql.SQL("operation = %s"))
        params.append(operation)

    clause = (
        sql.SQL(" where ") + sql.SQL(" and ").join(conditions)
        if conditions
        else sql.SQL("")
    )

    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("select count(*) from audit.event{}").format(clause), params
        )
        row = cur.fetchone()
        total = int(row[0]) if row else 0

        cur.execute(
            sql.SQL(
                "select {} from audit.event{} "
                "order by occurred_at desc, id desc limit %s offset %s"
            ).format(_COLUMNS, clause),
            [*params, PAGE_SIZE, max(0, offset)],
        )
        events = [_row(values) for values in cur.fetchall()]

    return events, total


def spend(conn: psycopg.Connection) -> Spend:
    """The totals shown above the table.

    One query rather than six. `filter` is how Postgres does a conditional
    aggregate, and it means the recent figures come from the same scan as the
    lifetime ones.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select
                count(*),
                coalesce(sum(cost_usd), 0),
                coalesce(sum(prompt_tokens + completion_tokens), 0),
                count(*) filter (where occurred_at > now() - interval '24 hours'),
                coalesce(sum(cost_usd) filter (where occurred_at > now() - interval '24 hours'), 0),
                coalesce(sum(prompt_tokens + completion_tokens)
                         filter (where occurred_at > now() - interval '24 hours'), 0)
            from audit.event
            """
        )
        values = cur.fetchone()

    if values is None:
        return Spend(0, Decimal(0), 0, 0, Decimal(0), 0)
    return Spend(
        events=int(values[0]),
        total_cost=values[1],
        total_tokens=int(values[2]),
        events_24h=int(values[3]),
        cost_24h=values[4],
        tokens_24h=int(values[5]),
    )


def operations(conn: psycopg.Connection) -> list[tuple[str, str, int]]:
    """Every (kind, operation) seen, with a count.

    The interface builds its filter from this rather than a hardcoded list, so
    a new operation appears in the dropdown the first time it happens without
    anyone remembering to add it.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select kind, operation, count(*)
            from audit.event
            group by kind, operation
            order by kind, operation
            """
        )
        return [(k, o, int(c)) for k, o, c in cur.fetchall()]


def by_actor(conn: psycopg.Connection, limit: int = 20) -> list[ActorSpend]:
    """Spend per person, dearest first.

    Grouped on the identity that asked rather than on the login, because the
    same person reaches Steven as a GitHub login on the dashboard and as a
    Discord user id in the server, and collapsing those two would need a
    mapping this layer has no business holding. They appear as two rows, each
    labelled with which surface it is.

    Rows with no cost at all are dropped: the trail records tool calls and
    slash commands too, and a list of people who spent nothing is noise on a
    panel whose whole subject is money. So is machine work — `actor_kind` is
    `system` for anything nobody asked for, and it is not a person. That is
    also why these figures need not sum to the total: the total is everything,
    this is only the part with a name against it.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select
                actor,
                actor_kind,
                count(*),
                coalesce(sum(cost_usd), 0),
                coalesce(sum(prompt_tokens + completion_tokens), 0),
                coalesce(sum(cost_usd) filter (
                    where occurred_at > now() - interval '24 hours'), 0),
                max(occurred_at)
            from audit.event
            where actor_kind <> 'system'
            group by actor, actor_kind
            having coalesce(sum(cost_usd), 0) > 0
            order by 4 desc
            limit %s
            """,
            [max(1, limit)],
        )
        return [
            ActorSpend(
                actor=actor,
                actor_kind=actor_kind,
                events=int(events),
                cost=cost,
                tokens=int(tokens),
                cost_24h=cost_24h,
                last_seen=last_seen,
            )
            for actor, actor_kind, events, cost, tokens, cost_24h, last_seen in cur.fetchall()
        ]
