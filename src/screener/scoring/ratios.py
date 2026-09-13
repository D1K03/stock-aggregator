"""Ten ratios, the rules that make one absent, and which apply to whom.

Yields rather than multiples (spec D2): a P/E of -5 is not cheaper than 10, and a
sorted multiple puts loss-makers at the top of Valuation. A yield is monotonic
through zero, so a loss-maker ranks as expensive.

Which ratios apply is decided by the level-2 industry, not the sector (D4):
Financial Services holds banks, whose debt is inventory, beside exchanges and
brokers, which are ordinary businesses. Percentiles stay at sector level.

A value that cannot be honestly computed is absent -- dropped from its pillar,
never imputed (D5). Nothing here raises on the shape of the data.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from screener.scoring.basis import Held, balance_at, flow_basis, market_cap, newest_balance

VALUATION = "valuation"
QUALITY = "quality"

STANDARD = "standard"
BALANCE_SHEET = "balance_sheet"
REIT = "reit"

# The same ten `metric.code` values migration 022 seeds, agreeing by hand.
RATIO_CODES: tuple[str, ...] = (
    "earnings_yield",
    "ebitda_ev",
    "fcf_yield",
    "book_yield",
    "ffo_yield",
    "roic",
    "roe",
    "gross_margin",
    "debt_to_equity",
    "interest_cover",
)

# `book_yield` and `roe` substitute rather than join every class: for an ordinary
# business ROE and ROIC measure one profitability twice, and a pillar average
# would count it double.
APPLICABLE: dict[str, dict[str, tuple[str, ...]]] = {
    STANDARD: {
        VALUATION: ("earnings_yield", "ebitda_ev", "fcf_yield"),
        QUALITY: ("roic", "gross_margin", "debt_to_equity", "interest_cover"),
    },
    BALANCE_SHEET: {
        VALUATION: ("earnings_yield", "book_yield"),
        QUALITY: ("roe",),
    },
    REIT: {
        VALUATION: ("ffo_yield", "ebitda_ev", "fcf_yield"),
        QUALITY: ("roic", "debt_to_equity", "interest_cover"),
    },
}

# Exists only to stop a one-off charge producing a tax rate above 100%.
TAX_RATE_CEILING = Decimal("0.5")


@dataclass(frozen=True)
class Ratio:
    value: Decimal
    # "TTM" or "A"; None for a ratio built only from balance items, which has no
    # basis to name (plan amendment A1).
    basis: str | None
    period_end: date


def industry_class(industry: str | None) -> str:
    """The class a level-2 industry code belongs to (spec D4)."""
    if industry is None:
        return STANDARD
    if industry.startswith("banks-") or industry == "mortgage-finance":
        return BALANCE_SHEET
    # Brokers sell insurance rather than underwrite it; their balance sheet is an
    # ordinary one.
    if industry.startswith("insurance-") and industry != "insurance-brokers":
        return BALANCE_SHEET
    if industry.startswith("reit-"):
        return REIT
    return STANDARD


def applicable(industry: str | None) -> dict[str, tuple[str, ...]]:
    """Each pillar's metrics that apply to this industry -- coverage's denominator."""
    return APPLICABLE[industry_class(industry)]


def _earnings_yield(held: Held, cap: Decimal | None, as_of: date) -> Ratio | None:
    if cap is None:
        return None
    basis = flow_basis(held, ("net_income",), as_of)
    if basis is None:
        return None
    return Ratio(basis.values["net_income"] / cap, basis.kind, basis.period_end)


def _ffo_yield(held: Held, cap: Decimal | None, as_of: date) -> Ratio | None:
    # Net income plus D&A: an approximation of funds from operations from stored
    # items, because depreciation on property that generally appreciates is what
    # makes a REIT's net income mislead.
    if cap is None:
        return None
    basis = flow_basis(held, ("net_income", "depreciation_amortisation"), as_of)
    if basis is None or basis.values["depreciation_amortisation"] < 0:
        return None
    ffo = basis.values["net_income"] + basis.values["depreciation_amortisation"]
    return Ratio(ffo / cap, basis.kind, basis.period_end)


def _ebitda_ev(held: Held, cap: Decimal | None, as_of: date) -> Ratio | None:
    if cap is None:
        return None
    basis = flow_basis(held, ("ebit", "depreciation_amortisation"), as_of)
    if basis is None or basis.values["depreciation_amortisation"] < 0:
        return None
    # Current, like the cap: enterprise value describes today (spec D7).
    found = newest_balance(held, ("total_debt", "cash_and_equivalents"), as_of)
    if found is None:
        return None
    balances = found[1]
    enterprise = cap + balances["total_debt"] - balances["cash_and_equivalents"]
    if enterprise <= 0:
        return None
    ebitda = basis.values["ebit"] + basis.values["depreciation_amortisation"]
    return Ratio(ebitda / enterprise, basis.kind, basis.period_end)


def _fcf_yield(held: Held, cap: Decimal | None, as_of: date) -> Ratio | None:
    if cap is None:
        return None
    basis = flow_basis(held, ("operating_cash_flow", "capital_expenditure"), as_of)
    # Yahoo reports capex negative for every security (F3), so free cash flow
    # *adds* it. A positive value means the convention moved, and should show as
    # lost coverage rather than silently halve FCF.
    if basis is None or basis.values["capital_expenditure"] > 0:
        return None
    fcf = basis.values["operating_cash_flow"] + basis.values["capital_expenditure"]
    return Ratio(fcf / cap, basis.kind, basis.period_end)


def _book_yield(held: Held, cap: Decimal | None, as_of: date) -> Ratio | None:
    if cap is None:
        return None
    found = newest_balance(held, ("stockholders_equity",), as_of)
    if found is None or found[1]["stockholders_equity"] <= 0:
        return None
    day, balances = found
    return Ratio(balances["stockholders_equity"] / cap, None, day)


def _tax_rate(tax: Decimal, pretax: Decimal) -> Decimal:
    # A negative provision is a tax benefit, not a negative rate.
    if pretax <= 0:
        return Decimal(0)
    return min(max(tax / pretax, Decimal(0)), TAX_RATE_CEILING)


def _roic(held: Held, cap: Decimal | None, as_of: date) -> Ratio | None:
    basis = flow_basis(held, ("ebit", "tax_provision", "pretax_income"), as_of)
    if basis is None:
        return None
    # At the basis's own date, so a return is measured on the capital of the
    # period that earned it (spec D7).
    balances = balance_at(
        held, ("stockholders_equity", "total_debt", "cash_and_equivalents"), basis.period_end
    )
    if balances is None:
        return None
    invested = (
        balances["stockholders_equity"]
        + balances["total_debt"]
        - balances["cash_and_equivalents"]
    )
    if invested <= 0:
        return None
    rate = _tax_rate(basis.values["tax_provision"], basis.values["pretax_income"])
    return Ratio(
        basis.values["ebit"] * (Decimal(1) - rate) / invested, basis.kind, basis.period_end
    )


def _roe(held: Held, cap: Decimal | None, as_of: date) -> Ratio | None:
    basis = flow_basis(held, ("net_income",), as_of)
    if basis is None:
        return None
    balances = balance_at(held, ("stockholders_equity",), basis.period_end)
    if balances is None or balances["stockholders_equity"] <= 0:
        return None
    return Ratio(
        basis.values["net_income"] / balances["stockholders_equity"], basis.kind, basis.period_end
    )


def _gross_margin(held: Held, cap: Decimal | None, as_of: date) -> Ratio | None:
    basis = flow_basis(held, ("gross_profit", "revenue"), as_of)
    if basis is None or basis.values["revenue"] <= 0:
        return None
    return Ratio(
        basis.values["gross_profit"] / basis.values["revenue"], basis.kind, basis.period_end
    )


def _debt_to_equity(held: Held, cap: Decimal | None, as_of: date) -> Ratio | None:
    # The newest date both are held, within 456 days (plan amendment A4). A
    # negative equity would rank a heavily indebted company as the least
    # leveraged, so it is absent instead.
    found = newest_balance(held, ("total_debt", "stockholders_equity"), as_of)
    if found is None or found[1]["stockholders_equity"] <= 0:
        return None
    day, balances = found
    return Ratio(balances["total_debt"] / balances["stockholders_equity"], None, day)


def _interest_cover(held: Held, cap: Decimal | None, as_of: date) -> Ratio | None:
    # A debt-free company loses this metric, but debt/equity of 0 still ranks it
    # best, so the signal survives in the pillar.
    basis = flow_basis(held, ("ebit", "interest_expense"), as_of)
    if basis is None or basis.values["interest_expense"] <= 0:
        return None
    return Ratio(
        basis.values["ebit"] / basis.values["interest_expense"], basis.kind, basis.period_end
    )


_FORMULAS: dict[str, Callable[[Held, Decimal | None, date], Ratio | None]] = {
    "earnings_yield": _earnings_yield,
    "ebitda_ev": _ebitda_ev,
    "fcf_yield": _fcf_yield,
    "book_yield": _book_yield,
    "ffo_yield": _ffo_yield,
    "roic": _roic,
    "roe": _roe,
    "gross_margin": _gross_margin,
    "debt_to_equity": _debt_to_equity,
    "interest_cover": _interest_cover,
}


def compute_ratios(
    held: Held,
    *,
    industry: str | None,
    close: Decimal | None,
    close_date: date | None,
    split_dates: Sequence[date],
    as_of: date,
) -> dict[str, Ratio]:
    """Every applicable ratio that can be honestly computed, keyed by code."""
    wanted = {code for codes in applicable(industry).values() for code in codes}
    cap = market_cap(held, close=close, close_date=close_date, split_dates=split_dates, as_of=as_of)
    out: dict[str, Ratio] = {}
    for code in RATIO_CODES:
        if code not in wanted:
            continue
        ratio = _FORMULAS[code](held, cap, as_of)
        if ratio is not None:
            out[code] = ratio
    return out
