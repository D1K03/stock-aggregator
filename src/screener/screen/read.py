"""The screen's reads on a connection, and the refusals they raise (ui-swap D7-D10).

On the connection the caller opens, which for the endpoints is the application's
own: the statements are fixed in `queries` and only bound parameters vary, which
is not what the playground's read-only roles exist to guard against (D6).
"""

from collections.abc import Mapping
from typing import Any, LiteralString, TypeVar

import psycopg

from screener.scoring import LOGIC_DESCRIPTION
from screener.screen import queries
from screener.screen.rows import PreviousRunRow, RunRow, parse_all

R = TypeVar("R")


class RunChanged(LookupError):
    """A pinned run no longer qualifies as a screen night (D8)."""

    def __init__(self, run_id: int) -> None:
        super().__init__(f"run {run_id} is not a scored v2 night")
        self.run_id = run_id


def _all(
    conn: psycopg.Connection, query: LiteralString, bind: Mapping[str, Any], record: type[R]
) -> list[R]:
    return parse_all(record, conn.execute(query, bind).fetchall())


def _one(
    conn: psycopg.Connection, query: LiteralString, bind: Mapping[str, Any], record: type[R]
) -> R | None:
    found = _all(conn, query, bind, record)
    return found[0] if found else None


def resolve_run(
    conn: psycopg.Connection, pinned: int | None
) -> tuple[RunRow, bool] | None:
    """The run to serve and whether it is the latest, or None when no v2 night exists.

    A pinned run goes on being served after a newer night lands, so paging through
    a screen never mixes two nights; it is refused only once it stops qualifying
    at all (D8).
    """
    bind = {"logic": LOGIC_DESCRIPTION, "run": pinned}
    latest = _one(conn, queries.LATEST_RUN, bind, RunRow)
    if pinned is None:
        return None if latest is None else (latest, True)
    run = _one(conn, queries.RUN_BY_ID, bind, RunRow)
    if run is None:
        raise RunChanged(pinned)
    return run, latest is not None and latest.id == run.id


def previous_run(conn: psycopg.Connection, run: RunRow) -> PreviousRunRow | None:
    """The night Δ is measured against: earlier, and under the same weights (D7)."""
    return _one(
        conn,
        queries.PREVIOUS_RUN,
        {"logic": LOGIC_DESCRIPTION, "as_of": run.as_of, "weight": run.weight_version_id},
        PreviousRunRow,
    )
