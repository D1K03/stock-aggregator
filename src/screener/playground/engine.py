"""Running one read-only query, and the only place SQL enters from outside.

Every other SQL site in this project keeps the statement text a literal and
varies only which fixed fragments are joined; `screener.migrate` holds the one
`cast(LiteralString, ...)`, on a versioned file out of `migrations/`. This
module is the exception, and the comment on `_as_query` is where it is argued
for rather than assumed.
"""

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as time_of_day, timedelta
from decimal import Decimal
from typing import Any, LiteralString, cast
from uuid import UUID

import psycopg

from screener.playground import config

logger = logging.getLogger(__name__)

# psycopg builds `DECLARE "name" CURSOR FOR ` + the text, with no separator. The
# newline keeps a query whose first line is a `--` comment from swallowing it,
# and both lengths are subtracted from any position Postgres reports so the
# caret lands under the character the reader actually typed.
_PREFIX = "\n"

# psycopg wraps the text as `DECLARE "<name>" CURSOR FOR ` — the parts joined
# with single spaces, and the FOR carrying a trailing one. Postgres reports an
# error position within *that* statement, so both this and the newline above come
# off it before the caret can land under the character the reader typed.
_CURSOR = "playground"
_DECLARE = len('DECLARE "') + len(_CURSOR) + len('" CURSOR FOR ')

# SQLSTATE classes that mean "the server", not "the query": 08 connection
# exception, 53 insufficient resources, 57P admin shutdown and crash recovery —
# deliberately not bare 57, because 57014 is a statement timeout and that is the
# query's business — 58 external system error, XX internal error.
_INFRASTRUCTURE = ("08", "53", "57P", "58", "XX")

_OPTIONS = (
    f"-c statement_timeout={config.STATEMENT_TIMEOUT_MS} "
    f"-c idle_in_transaction_session_timeout={config.IDLE_IN_TRANSACTION_TIMEOUT_MS} "
    f"-c lock_timeout={config.LOCK_TIMEOUT_MS} "
    "-c default_transaction_read_only=on "
    "-c search_path=public"
)


class NotConfigured(RuntimeError):
    """No read-only role is configured on this deployment."""


class Unavailable(RuntimeError):
    """The playground database could not be reached.

    Named by exception type only, never by message, for the reason
    `screener.health.checks` gives: psycopg embeds the host and usually the
    username in connection errors.
    """


class Misconfigured(RuntimeError):
    """The playground URL points at a privileged role.

    Nothing in the schema stops someone setting `PLAYGROUND_DATABASE_URL` to the
    application's own connection, and every test would still pass because the
    tests build their own. This is the check that does, and it refuses to serve
    rather than hand a superuser a SQL box.
    """


@dataclass(frozen=True, slots=True)
class QueryError(RuntimeError):
    """Postgres refused the query, and said why in terms about the query.

    The message is shown to the reader, unlike every other database error in
    this project. It has to be: `UndefinedColumn` with no position is a riddle,
    and "column security.tickr does not exist" with a caret under it is the
    entire value of a SQL box. It is safe to show precisely because it is about
    text the reader just typed.
    """

    message: str
    sqlstate: str | None = None
    position: int | None = None
    detail: str | None = None
    hint: str | None = None


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    type: str


@dataclass(frozen=True, slots=True)
class Result:
    columns: tuple[Column, ...]
    rows: tuple[tuple[Any, ...], ...]
    row_count: int
    truncated: bool
    shortened: int
    ms: int
    limit: int


def _as_query(text: str) -> LiteralString:
    """The one place in this project where SQL comes from outside it.

    psycopg types a query as `LiteralString` so that SQL assembled at runtime is
    a type error rather than a decision, and `screener.migrate` holds the only
    other `cast` in the tree, on a versioned file out of `migrations/`. This one
    is not trusted content and the cast does not pretend it is. What the cast
    records is that this module has stopped relying on the type system and put
    the enforcement somewhere the type system cannot see:

    * The connection is a role holding `select` on a listed set of tables and
      nothing else. It was never granted `auth.session` or `audit.event`, so
      there is no spelling of a query that reaches them — through a view, a CTE,
      a function or a cast nobody thought of. `pg_read_file` and `COPY FROM
      PROGRAM` are unreachable for the same reason: it is not a superuser and
      holds neither `pg_read_server_files` nor `pg_execute_server_program`.
    * The statement goes through a server-side cursor, so Postgres parses it as
      `DECLARE ... CURSOR FOR <this>` and accepts only a SELECT or VALUES,
      rejects data-modifying CTEs by name, and — because a DECLARE goes out over
      the extended protocol — cannot carry a second statement after a semicolon.
      That last one matters more than it looks: psycopg uses the *simple*
      protocol when a query has no parameters, and a playground query never has
      any, so a plain `execute` would run `select 1; drop table security`.
    * The transaction is `BEGIN READ ONLY`, on a role whose
      `default_transaction_read_only` is already on.

    A `text.upper().startswith("SELECT")` check here would be the opposite of
    all that. It would look like the safety while being one comment, one `WITH`
    or one leading space away from not being it.

    Rejected: `cur.execute(text.encode())`, which type-checks with no cast at
    all because `bytes` is in psycopg's `Query` union. It passes pyright and it
    is worse — `grep -rn "cast(LiteralString"` is the one command that finds
    every place SQL escapes the type system here, and that would not be found
    by it. The cast is the point of the cast.
    """
    return cast(LiteralString, _PREFIX + text)


def _cell(value: Any) -> Any:
    """One value, as something `json.dumps` can write without a `default=`.

    `screener.health.server._respond` deliberately has no `default=`, and every
    other endpoint converts by hand. Keeping that means the conversion lives
    here rather than becoming a licence for any endpoint to emit a Decimal.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        # NaN and the infinities are valid float8 and are not valid JSON.
        # `json.dumps` writes a bare NaN, which `JSON.parse` rejects, so one
        # cell would otherwise fail the whole response.
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Decimal):
        # A string, not a float. `price_daily.close` is numeric, and a figure
        # that changed in its seventh digit because it was rendered is exactly
        # the quiet wrongness this project spends its comments avoiding.
        return str(value)
    if isinstance(value, (datetime, date, time_of_day)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    if isinstance(value, (memoryview, bytes, bytearray)):
        return "\\x" + bytes(value).hex()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_cell(v) for v in value]
    if isinstance(value, dict):
        return value
    return str(value)


def _classify(exc: psycopg.Error) -> Exception:
    """A refused query, or an unreachable database. They are not the same thing.

    Split on SQLSTATE rather than on the exception class, because
    `QueryCanceled` — a statement timeout, which is entirely about the query —
    subclasses `OperationalError`. A class-based split would hide the one error
    a reader most needs to see.
    """
    state = exc.sqlstate
    if state is None or state.startswith(_INFRASTRUCTURE):
        return Unavailable(type(exc).__name__)
    position = None
    if exc.diag.statement_position:
        # Postgres counts from 1, and counts both prefixes this module added.
        position = max(
            0, int(exc.diag.statement_position) - 1 - len(_PREFIX) - _DECLARE
        )
    return QueryError(
        message=exc.diag.message_primary or str(exc),
        sqlstate=state,
        position=position,
        detail=exc.diag.message_detail,
        hint=exc.diag.message_hint,
    )


def connect() -> psycopg.Connection:
    """A read-only connection, refusing to be a privileged one."""
    url = config.database_url()
    if url is None:
        raise NotConfigured("no read-only database role is configured")
    try:
        conn = psycopg.connect(
            url,
            connect_timeout=config.CONNECT_TIMEOUT,
            options=_OPTIONS,
            application_name="screener-playground",
        )
    except psycopg.Error as exc:
        raise Unavailable(type(exc).__name__) from exc

    # Not cosmetic: psycopg emits a literal `BEGIN READ ONLY`, so read-only is a
    # property of the transaction rather than a session default something could
    # have flipped first.
    conn.read_only = True
    row = conn.execute(
        "select usesuper from pg_user where usename = current_user"
    ).fetchone()
    if row is not None and row[0]:
        conn.close()
        raise Misconfigured(
            "PLAYGROUND_DATABASE_URL points at a superuser; refusing to serve"
        )
    return conn


def run(sql_text: str, limit: int = config.DEFAULT_ROWS) -> Result:
    """Run one read-only query and return a bounded, JSON-safe result.

    `limit` is clamped here rather than trusted. That is not the contradiction
    of `audit.PAGE_SIZE` it looks like: a page size in a query string is a way
    to ask for a whole table at once, but this endpoint exists to run a query,
    so `limit 1000000` inside the SQL is the real risk — and the cursor bounds
    that regardless of what the caller asked for.

    Rows are fetched one over the limit, so `truncated` is a fact rather than a
    guess, and the rows past it are never sent over the wire.
    """
    text = sql_text.strip()
    if not text:
        raise QueryError("no query")
    if len(text) > config.MAX_SQL:
        raise QueryError(f"query longer than {config.MAX_SQL} characters")
    want = max(1, min(int(limit), config.MAX_ROWS))

    started = time.perf_counter()
    try:
        with connect() as conn:
            # A *named* cursor, which is the whole trick: it forces the extended
            # protocol, so a second statement after a semicolon is refused, and
            # it wraps the text in DECLARE ... CURSOR FOR, whose grammar accepts
            # only a SELECT or VALUES.
            with conn.cursor(name=_CURSOR) as cur:
                cur.itersize = want + 1
                cur.execute(_as_query(text))
                described = cur.description or ()
                columns = tuple(Column(d.name, _type_name(conn, d.type_code)) for d in described)
                fetched = cur.fetchmany(want + 1)
    except psycopg.Error as exc:
        raise _classify(exc) from exc

    truncated = len(fetched) > want
    rows: list[tuple[Any, ...]] = []
    shortened = 0
    budget = config.MAX_PAYLOAD
    for raw in fetched[:want]:
        out: list[Any] = []
        for value in raw:
            cell = _cell(value)
            if isinstance(cell, str) and len(cell) > config.MAX_CELL:
                cell = cell[: config.MAX_CELL] + "…"
                shortened += 1
            out.append(cell)
            budget -= len(cell) if isinstance(cell, str) else 16
        if budget < 0:
            # Stop adding rows rather than send a response nothing can hold.
            truncated = True
            break
        rows.append(tuple(out))

    return Result(
        columns=columns,
        rows=tuple(rows),
        row_count=len(rows),
        truncated=truncated,
        shortened=shortened,
        ms=int((time.perf_counter() - started) * 1000),
        limit=want,
    )


def _type_name(conn: psycopg.Connection, oid: int) -> str:
    """The type of a result column, as Postgres spells it.

    Looked up rather than guessed so the page can right-align numerics without
    a mapping of its own that would need a line per type.
    """
    row = conn.execute("select format_type(%s, null)", [oid]).fetchone()
    return str(row[0]) if row else "unknown"
