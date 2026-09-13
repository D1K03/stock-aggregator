"""Line items onto one period, so that no ratio mixes two.

A ratio built from inputs that are each fresh can still be wrong: this year's
operating cash flow less last year's capex is a number of the right size and the
wrong value. So a basis is chosen per ratio, over all of its flow inputs at once
(spec D6), and balance items are read at a date (D7).

Pure: plain values in, plain values out. `run.py` does the reading.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

# Constants in code rather than rows in the database: changing one changes what a
# metric means, so it bumps `scoring_logic_version` (spec D6). A weight only
# changes how much a metric counts.
TTM_MAX_AGE_DAYS = 135
# Every fiscal quarter of a 52/53-week year falls inside this.
QUARTER_GAP_MIN_DAYS = 80
QUARTER_GAP_MAX_DAYS = 100
ANNUAL_MAX_AGE_DAYS = 456
SPLIT_WINDOW_DAYS = 7
# A close older than a week cannot describe today's market cap; covers weekends
# and exchange holidays (spec amendment A5).
CLOSE_MAX_AGE_DAYS = 7

TTM = "TTM"
ANNUAL = "A"


@dataclass(frozen=True)
class Item:
    """One held fact, reduced to what basis assembly reads."""

    code: str
    period_end: date
    period_type: str
    value: Decimal
    currency: str | None


@dataclass(frozen=True)
class Basis:
    """Flow items on one period: four summed quarters, or one annual figure."""

    kind: str
    period_end: date
    values: dict[str, Decimal]


Held = Mapping[str, Sequence[Item]]


def index_facts(items: Iterable[Item], *, currency: str) -> dict[str, list[Item]]:
    """Facts by code, without any not in the security's currency (spec D9).

    Dropped here rather than checked per ratio, so a figure in another currency
    cannot reach a formula by any route: it simply is not held.
    """
    out: dict[str, list[Item]] = {}
    for item in items:
        if item.currency == currency:
            out.setdefault(item.code, []).append(item)
    return out


def _by_date(held: Held, code: str, period_type: str) -> dict[date, Decimal]:
    return {
        item.period_end: item.value
        for item in held.get(code, ())
        if item.period_type == period_type
    }


def _common_dates(series: Sequence[Mapping[date, Decimal]]) -> list[date]:
    """Dates every series holds, newest first."""
    if not series:
        return []
    return sorted(set(series[0]).intersection(*series[1:]), reverse=True)


def ttm_basis(held: Held, codes: Sequence[str], as_of: date) -> Basis | None:
    """Four consecutive quarters -- the same four for every code -- or None.

    Consecutiveness is checked on the dates, never assumed from the count: a
    security holding four quarters with one missing between them would otherwise
    sum fifteen months into a year (F1).
    """
    series = [_by_date(held, code, "Q") for code in codes]
    common = _common_dates(series)
    for i, end in enumerate(common):
        if end > as_of:
            continue
        if (as_of - end).days > TTM_MAX_AGE_DAYS:
            break
        four = common[i : i + 4]
        if len(four) < 4:
            break
        gaps = [(later - earlier).days for later, earlier in zip(four, four[1:])]
        if all(QUARTER_GAP_MIN_DAYS <= gap <= QUARTER_GAP_MAX_DAYS for gap in gaps):
            return Basis(
                TTM,
                end,
                {
                    code: sum((dated[day] for day in four), Decimal(0))
                    for code, dated in zip(codes, series)
                },
            )
    return None


def annual_basis(held: Held, codes: Sequence[str], as_of: date) -> Basis | None:
    """The newest annual period every code holds, if it is recent enough."""
    series = [_by_date(held, code, "A") for code in codes]
    for end in _common_dates(series):
        if end > as_of:
            continue
        if (as_of - end).days > ANNUAL_MAX_AGE_DAYS:
            break
        return Basis(ANNUAL, end, {code: dated[end] for code, dated in zip(codes, series)})
    return None


def flow_basis(held: Held, codes: Sequence[str], as_of: date) -> Basis | None:
    """TTM for every code, else one annual period for every code, else None."""
    return ttm_basis(held, codes, as_of) or annual_basis(held, codes, as_of)


def _balance_on(held: Held, code: str, day: date) -> Decimal | None:
    # A balance sheet describes a moment, so a quarterly and an annual value at one
    # date are one figure (spec D7). Annual first when both exist: it is the
    # audited statement.
    labelled = {item.period_type: item.value for item in held.get(code, ()) if item.period_end == day}
    return labelled.get("A", labelled.get("Q"))


def balance_at(held: Held, codes: Sequence[str], day: date) -> dict[str, Decimal] | None:
    """Every code's balance on one date, or None if any is missing."""
    out: dict[str, Decimal] = {}
    for code in codes:
        value = _balance_on(held, code, day)
        if value is None:
            return None
        out[code] = value
    return out


def newest_balance(
    held: Held,
    codes: Sequence[str],
    as_of: date,
    *,
    max_age_days: int = ANNUAL_MAX_AGE_DAYS,
) -> tuple[date, dict[str, Decimal]] | None:
    """The newest date at which every code is held, within the age bound."""
    if not codes:
        return None
    dates = sorted({item.period_end for item in held.get(codes[0], ())}, reverse=True)
    for day in dates:
        if day > as_of:
            continue
        if (as_of - day).days > max_age_days:
            break
        found = balance_at(held, codes, day)
        if found is not None:
            return day, found
    return None


def market_cap(
    held: Held,
    *,
    close: Decimal | None,
    close_date: date | None,
    split_dates: Sequence[date],
    as_of: date,
) -> Decimal | None:
    """Close times Yahoo's share count as it stands, or None (spec D8).

    **No split factor is ever applied.** Yahoo restates share counts for splits,
    so multiplying by the ratio again doubles the cap (F4). What is unsafe is the
    gap before it restates, so for a week after a split there is no market cap --
    a missing Valuation rather than a wrong one.

    The close itself is bounded too (spec amendment A5): a security whose bars
    stopped before a split pairs a pre-split close with a restated share count,
    a failing price ingest never writes split rows so that window never
    triggers, and a halted or acquired security stays valued on a months-old
    price. A close more than `CLOSE_MAX_AGE_DAYS` before `as_of` gives no
    market cap rather than a wrong one.
    """
    if close is None or close_date is None or (as_of - close_date).days > CLOSE_MAX_AGE_DAYS:
        return None
    window = timedelta(days=SPLIT_WINDOW_DAYS)
    if any(split <= as_of < split + window for split in split_dates):
        return None
    found = newest_balance(held, ("shares_outstanding",), as_of)
    if found is None:
        return None
    cap = close * found[1]["shares_outstanding"]
    return cap if cap > 0 else None
