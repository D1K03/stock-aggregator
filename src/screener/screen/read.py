"""The screen's reads on a connection, and the refusals they raise (ui-swap D7-D10).

On the connection the caller opens, which for the endpoints is the application's
own: the statements are fixed in `queries` and only bound parameters vary, which
is not what the playground's read-only roles exist to guard against (D6).
"""

from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any, LiteralString, TypeVar

import psycopg

from screener.scoring import LOGIC_DESCRIPTION
from screener.screen import queries, shape
from screener.screen.params import ScreenParams
from screener.screen.rows import (
    ActionRow,
    BarRow,
    PreviousRunRow,
    RunRow,
    ScreenRow,
    SectorRow,
    TilesRow,
    parse_all,
)

R = TypeVar("R")

SPARKLINE_CLOSES = 30
CHART_CLOSES = 60
# Sixty trading days is about 87 calendar days once weekends and holidays are
# counted. The bound exists so the read prunes partitions, not to cut the series.
CLOSE_LOOKBACK_DAYS = 120


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


def _closes(
    conn: psycopg.Connection, security_ids: Sequence[int], as_of: date, count: int
) -> dict[int, list[list[str]]]:
    """Adjusted display closes per security, from the bars stored now (D11).

    Unlike every scoring read there is no `observed_at` bound: ingest re-stamps
    the last week of bars nightly (F13), so bounding by the run's cutoff would
    empty the right edge of every chart until the next night was scored.
    """
    if not security_ids:
        return {}
    bind = {
        "ids": list(security_ids),
        "as_of": as_of,
        "since": as_of - timedelta(days=CLOSE_LOOKBACK_DAYS),
        "count": count,
    }
    bars: dict[int, list[BarRow]] = {}
    for bar in _all(conn, queries.CLOSES, bind, BarRow):
        bars.setdefault(bar.security_id, []).append(bar)
    actions: dict[int, list[ActionRow]] = {}
    for action in _all(conn, queries.ACTIONS, bind, ActionRow):
        actions.setdefault(action.security_id, []).append(action)
    return {
        security_id: shape.closes(bars.get(security_id, []), actions.get(security_id, []), count)
        for security_id in security_ids
    }


def read_screen(conn: psycopg.Connection, params: ScreenParams) -> dict[str, Any]:
    """`/api/screen`: one page of the served night (D9)."""
    resolved = resolve_run(conn, params.run)
    if resolved is None:
        return shape.awaiting()
    run, latest = resolved
    previous = previous_run(conn, run)
    bind = {
        "run": run.id,
        "as_of": run.as_of,
        "previous_run": None if previous is None else previous.id,
        "previous_as_of": None if previous is None else previous.as_of,
        "sector": params.sector,
        "agree": params.agree,
        "partial": params.partial,
        "offset": params.offset,
        "limit": params.limit,
    }
    tiles = _one(conn, queries.TILES, bind, TilesRow)
    # A select of scalar subqueries always returns its one row.
    assert tiles is not None
    counted = conn.execute(queries.SCREEN_COUNT, bind).fetchone()
    rows = _all(conn, queries.screen_page(params.sort), bind, ScreenRow)
    return shape.screen_payload(
        run=run,
        latest=latest,
        previous=previous,
        tiles=tiles,
        sectors=_all(conn, queries.SECTORS, bind, SectorRow),
        total=0 if counted is None else counted[0],
        rows=rows,
        closes_by_security=_closes(
            conn, [row.security_id for row in rows], run.as_of, SPARKLINE_CLOSES
        ),
    )
