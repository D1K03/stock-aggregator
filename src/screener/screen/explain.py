"""Reproducing one security's metrics as its run saw them (ui-swap spec D13, D14).

`reproduce` reads what the run could see and calls scoring's explaining forms;
`check` compares the result with what the run stored and gives each metric one
status. Percentiles are not re-derived -- they depend on every peer -- so `ok`
means the raw value reproduces, and no more.

**The run's view is `least(cutoff, started_at)`.** The nightly run starts about
23:00 on its date and its cutoff is 06:00 the next morning, so a manual ingest in
between writes rows the run never saw but a cutoff-bounded read would (F14).
Scoring's reads take a cutoff *offset*, so the view is expressed as the smaller
offset rather than as a second set of reads.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import psycopg

from screener.ingest import read_facts
from screener.scoring import (
    BAR_WINDOW_MONTHS,
    Absent,
    Action,
    Item,
    adjusted_closes,
    explain_momentum,
    explain_ratios,
    index_facts_explained,
    months_before,
    read_actions,
    read_bars,
    read_currencies,
)
from screener.screen import queries
from screener.screen.rows import RunRow

logger = logging.getLogger(__name__)

OK = "ok"
MISMATCH = "mismatch"
ABSENT = "absent"
UNEXPECTED = "unexpected"
REFRESHED = "refreshed"
UNCHECKED = "unchecked"

PRICE = "price"
FUNDAMENTALS = "fundamentals"

# D13: momentum reads bars; every Valuation ratio divides by a market cap built
# from a close as well as reading facts; Quality reads facts alone.
DEPENDS: dict[str, frozenset[str]] = {
    "momentum": frozenset({PRICE}),
    "valuation": frozenset({PRICE, FUNDAMENTALS}),
    "quality": frozenset({FUNDAMENTALS}),
}


@dataclass(frozen=True)
class Reproduction:
    visible_through: datetime
    # None when reproduction raised, which leaves every metric `unchecked` (spec §7).
    values: Mapping[str, Decimal | Absent] | None
    refreshed: frozenset[str]


@dataclass(frozen=True)
class Check:
    status: str
    reproduced: Decimal | None
    reason: str | None


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


def view_offset(run: RunRow) -> timedelta:
    """The cutoff offset under which scoring's reads see exactly what `run` could."""
    return min(
        timedelta(seconds=run.cutoff_offset_seconds), run.started_at - _midnight(run.as_of)
    )


def reproduce_values(
    bars: Sequence[tuple[date, Decimal, datetime]],
    actions: Sequence[Action],
    items: Sequence[Item],
    *,
    currency: str,
    industry: str | None,
    as_of: date,
) -> dict[str, Decimal | Absent]:
    """Every metric scoring would produce for one security, as a value or a reason.

    This repeats the per-security half of `screener.scoring.run.score` over the
    explaining forms rather than sharing it, because piece (a) was the one change
    to scoring the spec allows (§12). `test_every_stored_metric_reproduces` runs
    real scoring and requires every stored value to come back equal from here,
    which is what fails if the two drift (plan amendment P5).
    """
    out: dict[str, Decimal | Absent] = dict(
        explain_momentum(adjusted_closes(bars, actions), as_of)
    )
    held, dropped = index_facts_explained(items, currency=currency)
    ratios = explain_ratios(
        held,
        industry=industry,
        # The raw close of the latest visible bar, as `score` passes it.
        close=bars[-1][1] if bars else None,
        close_date=bars[-1][0] if bars else None,
        split_dates=[a.effective_date for a in actions if a.action_type == "split"],
        as_of=as_of,
        currency=currency,
        dropped=dropped,
    )
    for code, ratio in ratios.items():
        out[code] = ratio if isinstance(ratio, Absent) else ratio.value
    return out


def reproduce(
    conn: psycopg.Connection, *, security_id: int, run: RunRow, industry: str | None
) -> Reproduction:
    """Recompute one security under its run's view, and say which inputs changed since.

    Never raises: a failure is logged and returned as a reproduction that could
    not run, so the stored metrics are still shown. Call it after every other
    read on `conn`, because a failed statement leaves the transaction unusable.
    """
    offset = view_offset(run)
    visible_through = _midnight(run.as_of) + offset
    ids = [security_id]
    try:
        bars = read_bars(conn, ids, as_of=run.as_of, cutoff_offset=offset).get(security_id, [])
        actions = read_actions(conn, ids, as_of=run.as_of, cutoff_offset=offset).get(security_id, [])
        facts = read_facts(conn, ids, as_of=run.as_of, cutoff_offset=offset).get(security_id, [])
        values = reproduce_values(
            bars,
            actions,
            [Item(f.metric_code, f.period_end, f.period_type, f.value, f.currency) for f in facts],
            # Today's currency, not the run's: scoring does not read it point in
            # time either, and a change since is a named cause of `unexpected` (F15).
            currency=read_currencies(conn, ids)[security_id],
            industry=industry,
            as_of=run.as_of,
        )
        changed = conn.execute(
            queries.REFRESHED,
            {
                "id": security_id,
                "start": months_before(run.as_of, BAR_WINDOW_MONTHS),
                "as_of": run.as_of,
                "started_at": run.started_at,
            },
        ).fetchone()
    except Exception:
        logger.exception(
            "could not reproduce security %d on scoring run %d", security_id, run.id
        )
        return Reproduction(visible_through, None, frozenset())
    prices, fundamentals = changed if changed is not None else (False, False)
    refreshed = frozenset(
        name for name, flag in ((PRICE, prices), (FUNDAMENTALS, fundamentals)) if flag
    )
    return Reproduction(visible_through, values, refreshed)


def check(
    *, pillar: str, code: str, stored: Decimal | None, reproduction: Reproduction
) -> Check:
    """One metric's status: exactly one of D14's six."""
    if reproduction.values is None:
        return Check(UNCHECKED, None, None)
    if DEPENDS[pillar] & reproduction.refreshed:
        return Check(REFRESHED, None, None)
    value = reproduction.values.get(code)
    if value is None:
        # Stored by the run, but today's rules for this security's industry do
        # not produce it: it changed class since (plan amendment P4).
        value = Absent("no longer applicable to this security's industry")
    if isinstance(value, Absent):
        return Check(ABSENT if stored is None else MISMATCH, None, value.reason)
    if stored is None:
        return Check(UNEXPECTED, value, None)
    return Check(OK if value == stored else MISMATCH, value, None)
