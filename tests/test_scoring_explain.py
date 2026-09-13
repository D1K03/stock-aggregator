"""Why a metric is absent, from the same functions that decided it (ui-swap D12).

Nothing here changes what scoring writes -- tests/test_scoring_golden.py pins
that. These pin the reasons: one test per template, and the rule that when
several conditions would each make a metric absent, the first one in the
formula's evaluation order is the one reported.
"""

from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Sequence

from screener.scoring import (
    Absent,
    Item,
    explain_market_cap,
    first_missing_balance,
    index_facts,
    index_facts_explained,
    market_cap,
)

AS_OF = date(2026, 3, 2)
QUARTER = date(2025, 12, 31)


def shares(value: str = "100"):
    return index_facts([Item("shares_outstanding", QUARTER, "Q", Decimal(value), "USD")], currency="USD")


def cap(held: Any = None, *, close: str | None = "20", close_date: date | None = AS_OF, split_dates: Sequence[Any] = ()):
    return explain_market_cap(
        shares() if held is None else held,
        close=Decimal(close) if close is not None else None,
        close_date=close_date,
        split_dates=list(split_dates),
        as_of=AS_OF,
    )


# -- basis: index_facts ----------------------------------------------------------


def test_index_facts_explained_reports_the_currencies_it_dropped_per_code():
    items = [
        Item("net_income", QUARTER, "Q", Decimal(1), "EUR"),
        Item("net_income", date(2025, 9, 30), "Q", Decimal(1), "USD"),
        Item("revenue", QUARTER, "Q", Decimal(1), "AUD"),
        Item("revenue", date(2025, 9, 30), "Q", Decimal(1), None),
    ]

    held, foreign = index_facts_explained(items, currency="USD")

    assert [item.period_end for item in held["net_income"]] == [date(2025, 9, 30)]
    assert "revenue" not in held
    assert foreign == {"net_income": frozenset({"EUR"}), "revenue": frozenset({"AUD", "no currency"})}
    assert index_facts(items, currency="USD") == held


# -- basis: market cap -----------------------------------------------------------


def test_a_market_cap_that_can_be_computed_is_the_value():
    assert cap() == Decimal(2000)
    assert market_cap(shares(), close=Decimal(20), close_date=AS_OF, split_dates=[], as_of=AS_OF) == Decimal(2000)


def test_no_close_explains_itself():
    assert cap(close=None) == Absent("no market cap: no close visible")
    assert cap(close_date=None) == Absent("no market cap: no close visible")


def test_a_stale_close_says_how_old_it_is():
    assert cap(close_date=AS_OF - timedelta(days=8)) == Absent("no market cap: latest close is 8 days old")
    assert cap(close_date=AS_OF - timedelta(days=7)) == Decimal(2000)


def test_a_split_inside_its_window_names_the_split():
    assert cap(split_dates=[date(2026, 2, 28)]) == Absent("no market cap: split on 2026-02-28")


def test_no_recent_share_count_explains_itself():
    stale = index_facts([Item("shares_outstanding", date(2024, 9, 30), "Q", Decimal(100), "USD")], currency="USD")

    assert cap(stale) == Absent("no market cap: no share count within 15 months")


def test_a_non_positive_market_cap_explains_itself():
    assert cap(shares("0")) == Absent("no market cap: market cap ≤ 0")


def test_the_first_reason_wins_for_market_cap():
    # A stale close and a split in its window: the close is checked first.
    got = cap(close_date=AS_OF - timedelta(days=9), split_dates=[date(2026, 2, 28)])

    assert got == Absent("no market cap: latest close is 9 days old")


def test_first_missing_balance_names_the_first_item_absent_at_a_date():
    held = index_facts([Item("total_debt", QUARTER, "Q", Decimal(1), "USD")], currency="USD")

    assert first_missing_balance(held, ("total_debt", "stockholders_equity"), QUARTER) == "stockholders_equity"
    assert first_missing_balance(held, ("total_debt",), QUARTER) is None
