"""Line items onto one period, so a ratio never mixes two (spec D6-D9).

Every test here is a case where the naive read produces a number of the right
size and the wrong value -- which is why they are pure tests. A database test
would pass straight through each of them.
"""

from datetime import date, timedelta
from decimal import Decimal

from screener.scoring import (
    ANNUAL,
    TTM,
    Item,
    annual_basis,
    balance_at,
    flow_basis,
    index_facts,
    market_cap,
    newest_balance,
    ttm_basis,
)

AS_OF = date(2026, 3, 2)
# Gaps of 92, 92 and 91 days; the newest is 61 days before AS_OF.
QUARTERS = (date(2025, 12, 31), date(2025, 9, 30), date(2025, 6, 30), date(2025, 3, 31))


def q(code: str, end: date, value: str, currency: str = "USD") -> Item:
    return Item(code, end, "Q", Decimal(value), currency)


def a(code: str, end: date, value: str, currency: str = "USD") -> Item:
    return Item(code, end, "A", Decimal(value), currency)


def held(*items: Item):
    return index_facts(items, currency="USD")


def test_four_consecutive_quarters_sum_to_the_newest_end():
    facts = held(*(q("revenue", end, str(100 + i)) for i, end in enumerate(QUARTERS)))

    got = ttm_basis(facts, ("revenue",), AS_OF)

    assert got is not None
    assert (got.kind, got.period_end) == (TTM, date(2025, 12, 31))
    assert got.values == {"revenue": Decimal(406)}


def test_a_quarter_missing_between_four_held_is_not_summed_as_a_year():
    # Four quarters held, but June is missing and a year-old December stands in.
    # "The latest four" would sum fifteen months into a year (F1).
    facts = held(
        q("revenue", date(2025, 12, 31), "100"),
        q("revenue", date(2025, 9, 30), "100"),
        q("revenue", date(2025, 3, 31), "100"),
        q("revenue", date(2024, 12, 31), "100"),
    )

    assert ttm_basis(facts, ("revenue",), AS_OF) is None


def test_a_newest_quarter_older_than_135_days_is_not_ttm():
    facts = held(*(q("revenue", end, "100") for end in QUARTERS))

    assert ttm_basis(facts, ("revenue",), date(2026, 5, 15)) is not None  # 135 days
    assert ttm_basis(facts, ("revenue",), date(2026, 5, 20)) is None  # 140 days


def test_without_a_ttm_the_basis_falls_back_to_one_annual_period():
    facts = held(
        q("revenue", date(2025, 12, 31), "100"),
        q("revenue", date(2025, 9, 30), "100"),
        a("revenue", date(2025, 12, 31), "400"),
    )

    got = flow_basis(facts, ("revenue",), AS_OF)

    assert got is not None
    assert (got.kind, got.period_end, got.values) == (
        ANNUAL, date(2025, 12, 31), {"revenue": Decimal(400)}
    )


def test_an_annual_figure_is_eligible_for_456_days_and_not_one_more():
    facts = held(a("revenue", date(2025, 12, 31), "400"))

    assert annual_basis(facts, ("revenue",), date(2027, 4, 1)) is not None
    assert annual_basis(facts, ("revenue",), date(2027, 4, 2)) is None


def test_one_missing_capex_quarter_moves_the_whole_ratio_to_annual():
    # Operating cash flow alone assembles a TTM; with capex it cannot, and the
    # pair must not become this year's cash flow less last year's capex.
    facts = held(
        *(q("operating_cash_flow", end, "50") for end in QUARTERS),
        q("capital_expenditure", date(2025, 12, 31), "-20"),
        q("capital_expenditure", date(2025, 6, 30), "-20"),
        q("capital_expenditure", date(2025, 3, 31), "-20"),
        a("operating_cash_flow", date(2025, 12, 31), "190"),
        a("capital_expenditure", date(2025, 12, 31), "-85"),
    )

    alone = flow_basis(facts, ("operating_cash_flow",), AS_OF)
    together = flow_basis(facts, ("operating_cash_flow", "capital_expenditure"), AS_OF)

    assert alone is not None and alone.kind == TTM
    assert together is not None
    assert together.kind == ANNUAL
    assert together.values == {
        "operating_cash_flow": Decimal(190),
        "capital_expenditure": Decimal(-85),
    }


def test_a_fiscal_q4_and_its_year_at_one_period_end_stay_distinct():
    facts = held(
        *(q("revenue", end, str(100 + i)) for i, end in enumerate(QUARTERS)),
        a("revenue", date(2025, 12, 31), "400"),
    )

    ttm = ttm_basis(facts, ("revenue",), AS_OF)
    annual = annual_basis(facts, ("revenue",), AS_OF)

    assert ttm is not None and ttm.values == {"revenue": Decimal(406)}
    assert annual is not None and annual.values == {"revenue": Decimal(400)}


def test_a_balance_item_is_read_at_a_date_under_either_label():
    day = date(2025, 12, 31)

    assert balance_at(held(q("stockholders_equity", day, "800")), ("stockholders_equity",), day) == {
        "stockholders_equity": Decimal(800)
    }
    assert balance_at(held(a("stockholders_equity", day, "800")), ("stockholders_equity",), day) == {
        "stockholders_equity": Decimal(800)
    }
    assert balance_at(held(q("stockholders_equity", day, "800")), ("stockholders_equity",), date(2025, 9, 30)) is None


def test_the_newest_balance_date_is_one_where_every_item_is_held():
    facts = held(
        q("total_debt", date(2025, 12, 31), "400"),
        q("total_debt", date(2025, 9, 30), "390"),
        q("stockholders_equity", date(2025, 9, 30), "780"),
    )

    got = newest_balance(facts, ("total_debt", "stockholders_equity"), AS_OF)

    assert got == (
        date(2025, 9, 30),
        {"total_debt": Decimal(390), "stockholders_equity": Decimal(780)},
    )


def test_the_newest_balance_date_honours_its_age_bound():
    facts = held(q("total_debt", date(2024, 9, 30), "400"), q("stockholders_equity", date(2024, 9, 30), "800"))

    # 518 days: beyond 456 (plan amendment A4).
    assert newest_balance(facts, ("total_debt", "stockholders_equity"), AS_OF) is None


def test_market_cap_takes_the_share_count_as_it_stands():
    facts = held(q("shares_outstanding", date(2025, 12, 31), "100"))

    assert market_cap(facts, close=Decimal(20), split_dates=[], as_of=AS_OF) == Decimal(2000)


def test_a_split_never_multiplies_a_share_count():
    # Yahoo restates share counts for splits (F4). A split long enough ago to be
    # outside the window -- including one after the count's own date -- leaves
    # the count alone rather than doubling the cap.
    facts = held(q("shares_outstanding", date(2025, 12, 31), "100"))

    got = market_cap(facts, close=Decimal(20), split_dates=[date(2026, 1, 15)], as_of=AS_OF)

    assert got == Decimal(2000)


def test_market_cap_is_absent_for_seven_days_after_a_split():
    facts = held(q("shares_outstanding", date(2025, 12, 31), "100"))

    six_days_ago = AS_OF - timedelta(days=6)
    seven_days_ago = AS_OF - timedelta(days=7)

    assert market_cap(facts, close=Decimal(20), split_dates=[AS_OF], as_of=AS_OF) is None
    assert market_cap(facts, close=Decimal(20), split_dates=[six_days_ago], as_of=AS_OF) is None
    assert market_cap(facts, close=Decimal(20), split_dates=[seven_days_ago], as_of=AS_OF) == Decimal(2000)


def test_market_cap_is_absent_without_a_close_or_a_recent_share_count():
    fresh = held(q("shares_outstanding", date(2025, 12, 31), "100"))
    stale = held(q("shares_outstanding", date(2024, 9, 30), "100"))

    assert market_cap(fresh, close=None, split_dates=[], as_of=AS_OF) is None
    assert market_cap(stale, close=Decimal(20), split_dates=[], as_of=AS_OF) is None


def test_a_fact_in_another_currency_is_not_held():
    facts = index_facts(
        [
            q("revenue", QUARTERS[0], "100"),
            q("revenue", QUARTERS[1], "100", currency="EUR"),
            q("revenue", QUARTERS[2], "100"),
            q("revenue", QUARTERS[3], "100"),
        ],
        currency="USD",
    )

    assert [item.period_end for item in facts["revenue"]] == [QUARTERS[0], QUARTERS[2], QUARTERS[3]]
    assert ttm_basis(facts, ("revenue",), AS_OF) is None
