"""What Steven is allowed to spend, per person, per day.

The bill is shared and the model is cheap enough that nothing here is about
saving money in normal use — a reply costs a few hundredths of a penny. It is
about the abnormal case: a loop, a script, or someone finding out what happens
if they hold down enter. Without a ceiling the first anyone knows of it is the
invoice.

Counted per person rather than per identity, folding Discord onto GitHub
through the same `DISCORD_USER_MAP` the handoff uses, so the cap cannot be
doubled by asking the same question on the other surface.

It fails open. If the spend cannot be read — Postgres down, schema missing —
the question is answered anyway and a warning is logged, because refusing
everyone because the database blinked is a worse failure than a few unmetered
replies. That matches the rest of the audit layer, where recording never breaks
the operation it is recording.
"""

import logging
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import psycopg
from psycopg import sql

from screener.bot.config import BotConfig
from screener.config import settings

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT = 3

# Roughly two thousand replies a day at current prices, which is far more than
# two people can read and far less than a runaway loop can spend.
DEFAULT_DAILY_CAP_USD = Decimal("0.10")


def usd(value: Decimal) -> str:
    """A dollar figure that stays readable at these prices.

    Two decimal places renders a cap of a hundredth of a cent as "$0.00", which
    reads as a bug in the message that is meant to explain the refusal.
    """
    return f"${value:.2f}" if value >= Decimal("0.01") else f"${value:.4f}"


@dataclass(frozen=True, slots=True)
class Budget:
    """Where someone stands against the cap."""

    spent: Decimal
    cap: Decimal
    allowed: bool

    @property
    def remaining(self) -> Decimal:
        return max(Decimal(0), self.cap - self.spent)


def daily_cap() -> Decimal:
    """`DAILY_SPEND_CAP_USD`, in dollars.

    Zero means nobody may spend anything, which is the same reading
    `ALLOWED_DISCORD_USER_IDS` gives an empty list: the permissive
    interpretation turns a mistyped variable into no limit at all, which is the
    one outcome a cap exists to prevent.
    """
    raw = (os.environ.get("DAILY_SPEND_CAP_USD") or "").strip()
    if not raw:
        return DEFAULT_DAILY_CAP_USD
    try:
        value = Decimal(raw)
    except InvalidOperation:
        logger.warning("DAILY_SPEND_CAP_USD=%r is not a number; using the default", raw)
        return DEFAULT_DAILY_CAP_USD
    return max(Decimal(0), value)


def identities(actor: str, actor_kind: str) -> list[tuple[str, str]]:
    """Every (actor, actor_kind) belonging to the same person as this one.

    Always includes the pair it was given, so an unmapped account is still
    metered — under itself alone.
    """
    pairs = [(actor, actor_kind)]
    user_map = BotConfig.from_env().user_map

    if actor_kind == "github":
        discord_id = user_map.get(actor.casefold())
        if discord_id is not None:
            pairs.append((str(discord_id), "discord"))
    elif actor_kind == "discord":
        for login, discord_id in user_map.items():
            if str(discord_id) == actor:
                pairs.append((login, "github"))

    return pairs


def _query(pairs: list[tuple[str, str]]) -> sql.Composed:
    """Sum the last 24 hours for these identities.

    An `or` of matched pairs rather than two `in` lists, which would also match
    a Discord id that happened to equal somebody's login. Composed with
    `psycopg.sql` rather than interpolated, because psycopg types a query as
    `LiteralString` and a clause whose length depends on the data is exactly
    what that guardrail is for.
    """
    matches = sql.SQL(" or ").join(
        sql.SQL("(actor = %s and actor_kind = %s)") for _ in pairs
    )
    return sql.SQL(
        "select coalesce(sum(cost_usd), 0) from audit.event "
        "where occurred_at > now() - interval '24 hours' and ({})"
    ).format(matches)


def spent_24h(
    actor: str, actor_kind: str, *, conn: psycopg.Connection | None = None
) -> Decimal | None:
    """What this person has cost in the last 24 hours. `None` if unreadable.

    Takes an optional connection so a test can run the real query against a
    real schema. That seam is not decoration: the first version of this used a
    row comparison Postgres would not accept, every unit test stubbed this
    function out, and the cap failed open in silence for exactly as long as
    nothing executed the SQL.
    """
    pairs = identities(actor, actor_kind)
    params = [value for pair in pairs for value in pair]
    try:
        if conn is not None:
            row = conn.execute(_query(pairs), params).fetchone()
            return row[0] if row else Decimal(0)
        with psycopg.connect(
            settings().database_url, connect_timeout=CONNECT_TIMEOUT
        ) as owned:
            row = owned.execute(_query(pairs), params).fetchone()
            return row[0] if row else Decimal(0)
    except Exception as exc:
        logger.warning("could not read spend for %s: %s", actor, exc)
        return None


def check(actor: str, actor_kind: str) -> Budget:
    """Whether this person may ask another question.

    System work is never capped: it is not a person, nobody is holding down
    enter, and refusing a scheduled job on a budget meant for chat would be a
    surprise at the worst possible time.
    """
    cap = daily_cap()
    if actor_kind == "system":
        return Budget(spent=Decimal(0), cap=cap, allowed=True)

    spent = spent_24h(actor, actor_kind)
    if spent is None:
        # Unreadable. Answer anyway; the warning is already logged.
        return Budget(spent=Decimal(0), cap=cap, allowed=True)
    return Budget(spent=spent, cap=cap, allowed=spent < cap)
