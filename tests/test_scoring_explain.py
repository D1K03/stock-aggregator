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
    Ratio,
    compute,
    explain_market_cap,
    explain_momentum,
    explain_ratios,
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


def test_index_facts_explained_reports_the_dropped_facts_per_code():
    eur_income = Item("net_income", QUARTER, "Q", Decimal(1), "EUR")
    aud_revenue = Item("revenue", QUARTER, "Q", Decimal(1), "AUD")
    no_currency_revenue = Item("revenue", date(2025, 9, 30), "Q", Decimal(1), None)
    items = [
        eur_income,
        Item("net_income", date(2025, 9, 30), "Q", Decimal(1), "USD"),
        aud_revenue,
        no_currency_revenue,
    ]

    held, dropped = index_facts_explained(items, currency="USD")

    assert [item.period_end for item in held["net_income"]] == [date(2025, 9, 30)]
    assert "revenue" not in held
    assert dropped == {"net_income": [eur_income], "revenue": [aud_revenue, no_currency_revenue]}
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


# -- ratios --------------------------------------------------------------------

QUARTERS = (date(2025, 12, 31), date(2025, 9, 30), date(2025, 6, 30), date(2025, 3, 31))
FLOWS = {
    "net_income": "25", "revenue": "250", "gross_profit": "100", "ebit": "40",
    "depreciation_amortisation": "10", "operating_cash_flow": "50",
    "capital_expenditure": "-20", "interest_expense": "5", "tax_provision": "8",
    "pretax_income": "32",
}
BALANCES = {
    "stockholders_equity": "800", "total_debt": "400", "cash_and_equivalents": "200",
    "shares_outstanding": "100",
}


def company_items(*, drop=(), currency_of=None, **overrides):
    """The hand-checkable company of test_scoring_ratios.py, as raw items.

    `drop` removes line items entirely; `currency_of` reports named items in
    another currency; overrides replace per-quarter flow or balance values.
    """
    currency_of = currency_of or {}
    items = []
    for code, value in FLOWS.items():
        if code in drop:
            continue
        for end in QUARTERS:
            items.append(Item(code, end, "Q", Decimal(overrides.get(code, value)), currency_of.get(code, "USD")))
    for code, value in BALANCES.items():
        if code in drop:
            continue
        items.append(Item(code, QUARTERS[0], "Q", Decimal(overrides.get(code, value)), currency_of.get(code, "USD")))
    return items


def explain(
    industry="software",
    close: str | None = "20",
    *,
    drop=(),
    currency_of=None,
    extra_items=(),
    **overrides,
):
    held, dropped = index_facts_explained(
        [*company_items(drop=drop, currency_of=currency_of, **overrides), *extra_items],
        currency="USD",
    )
    return explain_ratios(
        held,
        industry=industry,
        close=Decimal(close) if close is not None else None,
        close_date=AS_OF,
        split_dates=[],
        as_of=AS_OF,
        currency="USD",
        dropped=dropped,
    )


def test_a_computable_ratio_is_its_ratio_and_only_applicable_codes_appear():
    bank = explain(industry="banks-regional")

    assert set(bank) == {"earnings_yield", "book_yield", "roe"}
    assert bank["earnings_yield"] == Ratio(Decimal("0.05"), "TTM", date(2025, 12, 31))


def test_no_market_cap_is_the_reason_for_every_valuation_ratio_and_quality_stands():
    got = explain(close=None)

    for code in ("earnings_yield", "ebitda_ev", "fcf_yield"):
        assert got[code] == Absent("no market cap: no close visible")
    assert isinstance(got["roic"], Ratio)


def test_the_first_reason_wins_across_market_cap_and_basis():
    # No close and no net income: market cap is checked first.
    got = explain(close=None, drop=("net_income",))

    assert got["earnings_yield"] == Absent("no market cap: no close visible")


def test_a_missing_basis_names_the_inputs():
    got = explain(drop=("interest_expense",))

    assert got["interest_cover"] == Absent("no clean TTM or annual figure for ebit, interest_expense")


def test_facts_in_another_currency_are_the_reason_rather_than_no_figure():
    got = explain(currency_of={"net_income": "EUR"})

    assert got["earnings_yield"] == Absent("facts reported in EUR, not USD")


def test_an_old_irrelevant_foreign_fact_does_not_blame_the_currency():
    # The dropped AUD fact is 2021 annual, far outside the 456-day annual window,
    # and ebit has no annual figures at all: including it would still leave
    # interest_cover with no basis, so the ordinary reason stands.
    got = explain(
        drop=("interest_expense",),
        extra_items=[Item("interest_expense", date(2021, 12, 31), "A", Decimal(5), "AUD")],
    )

    assert got["interest_cover"] == Absent("no clean TTM or annual figure for ebit, interest_expense")


def test_an_irrelevant_foreign_fact_at_the_at_path_does_not_blame_the_currency():
    # The dropped EUR fact is total_debt at 2022-12-31, not the code and date
    # roic is actually missing: cash_and_equivalents at 2025-12-31.
    got = explain(
        drop=("cash_and_equivalents",),
        extra_items=[Item("total_debt", date(2022, 12, 31), "A", Decimal(400), "EUR")],
    )

    assert got["roic"] == Absent("no cash_and_equivalents at 2025-12-31")


def test_a_relevant_foreign_fact_at_the_at_path_is_blamed():
    got = explain(currency_of={"stockholders_equity": "EUR"})

    assert got["roic"] == Absent("facts reported in EUR, not USD")


def test_a_relevant_foreign_fact_at_the_newest_path_is_blamed():
    got = explain(currency_of={"total_debt": "EUR"})

    assert got["debt_to_equity"] == Absent("facts reported in EUR, not USD")


def test_a_relevant_foreign_fact_is_blamed_for_market_cap():
    held, dropped = index_facts_explained(
        [Item("shares_outstanding", QUARTER, "Q", Decimal(100), "AUD")], currency="USD"
    )

    got = explain_market_cap(
        held,
        close=Decimal(20),
        close_date=AS_OF,
        split_dates=[],
        as_of=AS_OF,
        currency="USD",
        dropped=dropped,
    )

    assert got == Absent("no market cap: facts reported in AUD, not USD")


def test_no_currency_or_dropped_context_gives_the_ordinary_reason():
    held, _ = index_facts_explained(
        company_items(currency_of={"net_income": "EUR"}), currency="USD"
    )

    got = explain_ratios(
        held,
        industry="software",
        close=Decimal("20"),
        close_date=AS_OF,
        split_dates=[],
        as_of=AS_OF,
        currency=None,
        dropped=None,
    )

    assert got["earnings_yield"] == Absent("no clean TTM or annual figure for net_income")


def test_a_dropped_fact_with_no_currency_names_it_unknown():
    extra = [Item("interest_expense", end, "Q", Decimal("5"), None) for end in QUARTERS]

    got = explain(drop=("interest_expense",), extra_items=extra)

    assert got["interest_cover"] == Absent("facts reported in an unknown currency, not USD")


def test_a_missing_newest_balance_names_every_item_it_needed():
    got = explain(drop=("total_debt",))

    assert got["debt_to_equity"] == Absent(
        "no date within 15 months holding all of total_debt, stockholders_equity"
    )


def test_a_balance_missing_at_the_basis_date_names_the_item_and_date():
    got = explain(drop=("cash_and_equivalents",))

    assert got["roic"] == Absent("no cash_and_equivalents at 2025-12-31")


def test_each_sign_rule_has_its_reason():
    assert explain(stockholders_equity="-100")["debt_to_equity"] == Absent("equity ≤ 0")
    assert explain(cash_and_equivalents="3000")["ebitda_ev"] == Absent("enterprise value ≤ 0")
    assert explain(cash_and_equivalents="3000")["roic"] == Absent("invested capital ≤ 0")
    assert explain(interest_expense="0")["interest_cover"] == Absent("interest expense ≤ 0")
    assert explain(depreciation_amortisation="-10")["ebitda_ev"] == Absent("D&A negative")
    assert explain(industry="reit-retail", depreciation_amortisation="-10")["ffo_yield"] == Absent("D&A negative")
    assert explain(capital_expenditure="20")["fcf_yield"] == Absent("capex positive")
    assert explain(revenue="0")["gross_margin"] == Absent("revenue ≤ 0")
    bank = explain(industry="banks-regional", stockholders_equity="-100")
    assert bank["roe"] == Absent("equity ≤ 0")
    assert bank["book_yield"] == Absent("equity ≤ 0")


# -- momentum ------------------------------------------------------------------


def bars(*pairs):
    """(days before AS_OF, close) pairs into a series."""
    return [(AS_OF - timedelta(days=days), Decimal(close)) for days, close in pairs]


def test_no_visible_bars_is_the_reason_for_all_four():
    assert explain_momentum([], AS_OF) == {
        code: Absent("no visible bars") for code in ("ret_3m", "ret_6m", "ret_12m", "off_52w_high")
    }


def test_a_short_history_names_each_window_it_does_not_cover():
    got = explain_momentum(bars((100, "100"), (0, "110")), AS_OF)

    assert got["ret_3m"] == Decimal("0.1")
    assert got["ret_6m"] == Absent("price history shorter than the 6-month window")
    assert got["ret_12m"] == Absent("price history shorter than the 12-month window")
    assert got["off_52w_high"] == Absent("price history shorter than the 52-week window")


def test_a_non_positive_past_close_or_high_has_its_reason():
    got = explain_momentum(bars((380, "0"), (200, "0"), (100, "0"), (0, "0")), AS_OF)

    assert got["ret_3m"] == Absent("a past close ≤ 0")
    assert got["off_52w_high"] == Absent("52-week high ≤ 0")


def test_an_empty_52_week_window_is_absent_instead_of_crashing_the_night():
    # Bars only between 52 weeks and 13 months old: inside read_bars' window,
    # outside the 52-week one. Before plan amendment P1 this raised ValueError
    # from max() and failed every security's night.
    series = bars((380, "100"), (370, "101"))

    got = explain_momentum(series, AS_OF)

    assert got["off_52w_high"] == Absent("no bars in the 52-week window")
    assert "off_52w_high" not in compute(series, AS_OF)


def test_compute_is_explain_momentum_without_the_absences():
    series = bars((380, "100"), (200, "90"), (100, "95"), (30, "120"), (0, "110"))

    explained = explain_momentum(series, AS_OF)

    assert all(isinstance(value, Decimal) for value in explained.values())
    assert compute(series, AS_OF) == explained
