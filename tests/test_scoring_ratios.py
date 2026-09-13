"""Ten ratios, when each is absent, and which apply to whom (spec D2-D5).

One company, built so every ratio is checkable by eye. Per quarter: net income
25, revenue 250, gross profit 100, EBIT 40, D&A 10, operating cash flow 50,
capex -20, interest 5, tax 8, pre-tax 32. At 2025-12-31: equity 800, debt 400,
cash 200, shares 100. Close 20, so market cap 2,000.
"""

from datetime import date
from decimal import Decimal

from screener.scoring import (
    BALANCE_SHEET,
    REIT,
    STANDARD,
    TTM,
    Item,
    applicable,
    compute_ratios,
    index_facts,
    industry_class,
    percentiles,
)

AS_OF = date(2026, 3, 2)
QUARTERS = (date(2025, 12, 31), date(2025, 9, 30), date(2025, 6, 30), date(2025, 3, 31))
FLOWS = {
    "net_income": "25",
    "revenue": "250",
    "gross_profit": "100",
    "ebit": "40",
    "depreciation_amortisation": "10",
    "operating_cash_flow": "50",
    "capital_expenditure": "-20",
    "interest_expense": "5",
    "tax_provision": "8",
    "pretax_income": "32",
}
BALANCES = {
    "stockholders_equity": "800",
    "total_debt": "400",
    "cash_and_equivalents": "200",
    "shares_outstanding": "100",
}


def company(**overrides: str):
    """The company above, with any per-quarter flow or balance value replaced."""
    flows = {code: overrides.get(code, value) for code, value in FLOWS.items()}
    balances = {code: overrides.get(code, value) for code, value in BALANCES.items()}
    items = [
        Item(code, end, "Q", Decimal(value), "USD")
        for code, value in flows.items()
        for end in QUARTERS
    ]
    items += [
        Item(code, QUARTERS[0], "Q", Decimal(value), "USD")
        for code, value in balances.items()
    ]
    return index_facts(items, currency="USD")


def ratios(industry: str | None = "software", close: str | None = "20", **overrides: str):
    return compute_ratios(
        company(**overrides),
        industry=industry,
        close=Decimal(close) if close is not None else None,
        split_dates=[],
        as_of=AS_OF,
    )


def values(got):
    return {code: ratio.value for code, ratio in got.items()}


def test_a_standard_company_gets_its_seven_ratios_by_hand():
    got = values(ratios())

    assert got == {
        "earnings_yield": Decimal("0.05"),  # 100 / 2000
        "ebitda_ev": Decimal(200) / Decimal(2200),  # (160 + 40) / (2000 + 400 - 200)
        "fcf_yield": Decimal("0.06"),  # (200 + -80) / 2000
        "roic": Decimal("0.12"),  # 160 x (1 - 32/128) / (800 + 400 - 200)
        "gross_margin": Decimal("0.4"),  # 400 / 1000
        "debt_to_equity": Decimal("0.5"),  # 400 / 800
        "interest_cover": Decimal(8),  # 160 / 20
    }


def test_free_cash_flow_adds_capex_because_yahoo_reports_it_negative():
    # OCF - capex would be (200 - -80) / 2000 = 0.14 (F3).
    assert ratios()["fcf_yield"].value == Decimal("0.06")
    assert "fcf_yield" not in ratios(capital_expenditure="20")


def test_a_bank_gets_book_yield_and_roe_in_place_of_what_means_nothing_for_it():
    got = values(ratios(industry="banks-regional"))

    assert got == {
        "earnings_yield": Decimal("0.05"),
        "book_yield": Decimal("0.4"),  # 800 / 2000
        "roe": Decimal("0.125"),  # 100 / 800
    }


def test_a_reit_gets_ffo_yield_and_no_earnings_yield_or_gross_margin():
    got = values(ratios(industry="reit-retail"))

    assert set(got) == {"ffo_yield", "ebitda_ev", "fcf_yield", "roic", "debt_to_equity", "interest_cover"}
    assert got["ffo_yield"] == Decimal("0.07")  # (100 + 40) / 2000


def test_industries_map_to_classes_and_brokers_are_ordinary_businesses():
    assert industry_class("banks-regional") == BALANCE_SHEET
    assert industry_class("banks-diversified") == BALANCE_SHEET
    assert industry_class("insurance-life") == BALANCE_SHEET
    assert industry_class("mortgage-finance") == BALANCE_SHEET
    assert industry_class("insurance-brokers") == STANDARD
    assert industry_class("reit-office") == REIT
    assert industry_class("credit-services") == STANDARD
    assert industry_class("software") == STANDARD
    assert industry_class(None) == STANDARD


def test_applicable_lists_each_pillars_metrics_for_a_class():
    assert applicable("banks-regional") == {
        "valuation": ("earnings_yield", "book_yield"),
        "quality": ("roe",),
    }
    assert len(applicable("software")["quality"]) == 4


def test_negative_equity_makes_every_ratio_divided_by_it_absent():
    standard = ratios(stockholders_equity="-100")
    bank = ratios(industry="banks-regional", stockholders_equity="-100")

    assert "debt_to_equity" not in standard
    assert "roe" not in bank and "book_yield" not in bank


def test_a_non_positive_enterprise_value_or_invested_capital_is_absent():
    got = ratios(cash_and_equivalents="3000")  # EV = 2000 + 400 - 3000; IC = 800 + 400 - 3000

    assert "ebitda_ev" not in got
    assert "roic" not in got


def test_the_tax_rate_is_clamped_and_is_zero_for_a_pre_tax_loss():
    assert ratios(tax_provision="80")["roic"].value == Decimal("0.08")  # t = 2.5 -> 0.5
    assert ratios(tax_provision="-8")["roic"].value == Decimal("0.16")  # t < 0 -> 0
    assert ratios(pretax_income="-10")["roic"].value == Decimal("0.16")  # loss -> 0


def test_zero_interest_negative_da_and_zero_revenue_are_absences():
    assert "interest_cover" not in ratios(interest_expense="0")
    assert "ebitda_ev" not in ratios(depreciation_amortisation="-10")
    assert "ffo_yield" not in ratios(industry="reit-retail", depreciation_amortisation="-10")
    assert "gross_margin" not in ratios(revenue="0")


def test_without_a_market_cap_valuation_is_absent_and_quality_stands():
    got = ratios(close=None)

    assert not {"earnings_yield", "ebitda_ev", "fcf_yield"} & set(got)
    assert {"roic", "gross_margin", "debt_to_equity", "interest_cover"} <= set(got)


def test_a_loss_maker_ranks_below_a_profitable_peer_rather_than_on_top():
    # Why yields rather than multiples (spec D2).
    loss = ratios(net_income="-25")["earnings_yield"].value
    profit = ratios()["earnings_yield"].value

    assert loss == Decimal("-0.05")
    assert percentiles([loss, profit]) == [Decimal(0), Decimal(100)]


def test_each_ratio_records_its_basis_and_a_balance_only_ratio_records_none():
    got = ratios()

    assert (got["earnings_yield"].basis, got["earnings_yield"].period_end) == (TTM, date(2025, 12, 31))
    assert (got["debt_to_equity"].basis, got["debt_to_equity"].period_end) == (None, date(2025, 12, 31))
