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

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from screener.scoring.basis import (
    Absent,
    Basis,
    Held,
    balance_at,
    explain_market_cap,
    first_missing_balance,
    flow_basis,
    newest_balance,
)

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


Explained = Ratio | Absent


@dataclass(frozen=True)
class _Inputs:
    """What every formula reads, and what it needs to explain an absence."""

    held: Held
    as_of: date
    currency: str | None
    foreign: Mapping[str, frozenset[str]]


def _unavailable(inputs: _Inputs, codes: Sequence[str], reason: str) -> Absent:
    # A figure reported in another currency was dropped before any formula saw
    # it, so "no figure" would be the wrong story: say where it went instead.
    dropped = sorted({c for code in codes for c in inputs.foreign.get(code, frozenset())})
    if dropped and inputs.currency is not None:
        return Absent(f"facts reported in {', '.join(dropped)}, not {inputs.currency}")
    return Absent(reason)


def _basis(inputs: _Inputs, codes: Sequence[str]) -> Basis | Absent:
    basis = flow_basis(inputs.held, codes, inputs.as_of)
    if basis is None:
        return _unavailable(
            inputs, codes, f"no clean TTM or annual figure for {', '.join(codes)}"
        )
    return basis


def _newest(inputs: _Inputs, codes: Sequence[str]) -> tuple[date, dict[str, Decimal]] | Absent:
    found = newest_balance(inputs.held, codes, inputs.as_of)
    if found is None:
        return _unavailable(
            inputs, codes, f"no date within 15 months holding all of {', '.join(codes)}"
        )
    return found


def _at(inputs: _Inputs, codes: Sequence[str], day: date) -> dict[str, Decimal] | Absent:
    balances = balance_at(inputs.held, codes, day)
    if balances is None:
        missing = first_missing_balance(inputs.held, codes, day) or ", ".join(codes)
        return _unavailable(inputs, codes, f"no {missing} at {day.isoformat()}")
    return balances


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


def _earnings_yield(inputs: _Inputs, cap: Decimal | Absent) -> Explained:
    if isinstance(cap, Absent):
        return cap
    basis = _basis(inputs, ("net_income",))
    if isinstance(basis, Absent):
        return basis
    return Ratio(basis.values["net_income"] / cap, basis.kind, basis.period_end)


def _ffo_yield(inputs: _Inputs, cap: Decimal | Absent) -> Explained:
    # Net income plus D&A: an approximation of funds from operations from stored
    # items, because depreciation on property that generally appreciates is what
    # makes a REIT's net income mislead.
    if isinstance(cap, Absent):
        return cap
    basis = _basis(inputs, ("net_income", "depreciation_amortisation"))
    if isinstance(basis, Absent):
        return basis
    if basis.values["depreciation_amortisation"] < 0:
        return Absent("D&A negative")
    ffo = basis.values["net_income"] + basis.values["depreciation_amortisation"]
    return Ratio(ffo / cap, basis.kind, basis.period_end)


def _ebitda_ev(inputs: _Inputs, cap: Decimal | Absent) -> Explained:
    if isinstance(cap, Absent):
        return cap
    basis = _basis(inputs, ("ebit", "depreciation_amortisation"))
    if isinstance(basis, Absent):
        return basis
    if basis.values["depreciation_amortisation"] < 0:
        return Absent("D&A negative")
    # Current, like the cap: enterprise value describes today (spec D7).
    found = _newest(inputs, ("total_debt", "cash_and_equivalents"))
    if isinstance(found, Absent):
        return found
    balances = found[1]
    enterprise = cap + balances["total_debt"] - balances["cash_and_equivalents"]
    if enterprise <= 0:
        return Absent("enterprise value ≤ 0")
    ebitda = basis.values["ebit"] + basis.values["depreciation_amortisation"]
    return Ratio(ebitda / enterprise, basis.kind, basis.period_end)


def _fcf_yield(inputs: _Inputs, cap: Decimal | Absent) -> Explained:
    if isinstance(cap, Absent):
        return cap
    basis = _basis(inputs, ("operating_cash_flow", "capital_expenditure"))
    if isinstance(basis, Absent):
        return basis
    # Yahoo reports capex negative for every security (F3), so free cash flow
    # *adds* it. A positive value means the convention moved, and should show as
    # lost coverage rather than silently halve FCF.
    if basis.values["capital_expenditure"] > 0:
        return Absent("capex positive")
    fcf = basis.values["operating_cash_flow"] + basis.values["capital_expenditure"]
    return Ratio(fcf / cap, basis.kind, basis.period_end)


def _book_yield(inputs: _Inputs, cap: Decimal | Absent) -> Explained:
    if isinstance(cap, Absent):
        return cap
    found = _newest(inputs, ("stockholders_equity",))
    if isinstance(found, Absent):
        return found
    day, balances = found
    if balances["stockholders_equity"] <= 0:
        return Absent("equity ≤ 0")
    return Ratio(balances["stockholders_equity"] / cap, None, day)


def _tax_rate(tax: Decimal, pretax: Decimal) -> Decimal:
    # A negative provision is a tax benefit, not a negative rate.
    if pretax <= 0:
        return Decimal(0)
    return min(max(tax / pretax, Decimal(0)), TAX_RATE_CEILING)


def _roic(inputs: _Inputs, cap: Decimal | Absent) -> Explained:
    basis = _basis(inputs, ("ebit", "tax_provision", "pretax_income"))
    if isinstance(basis, Absent):
        return basis
    # At the basis's own date, so a return is measured on the capital of the
    # period that earned it (spec D7).
    balances = _at(
        inputs, ("stockholders_equity", "total_debt", "cash_and_equivalents"), basis.period_end
    )
    if isinstance(balances, Absent):
        return balances
    invested = (
        balances["stockholders_equity"]
        + balances["total_debt"]
        - balances["cash_and_equivalents"]
    )
    if invested <= 0:
        return Absent("invested capital ≤ 0")
    rate = _tax_rate(basis.values["tax_provision"], basis.values["pretax_income"])
    return Ratio(
        basis.values["ebit"] * (Decimal(1) - rate) / invested, basis.kind, basis.period_end
    )


def _roe(inputs: _Inputs, cap: Decimal | Absent) -> Explained:
    basis = _basis(inputs, ("net_income",))
    if isinstance(basis, Absent):
        return basis
    balances = _at(inputs, ("stockholders_equity",), basis.period_end)
    if isinstance(balances, Absent):
        return balances
    if balances["stockholders_equity"] <= 0:
        return Absent("equity ≤ 0")
    return Ratio(
        basis.values["net_income"] / balances["stockholders_equity"], basis.kind, basis.period_end
    )


def _gross_margin(inputs: _Inputs, cap: Decimal | Absent) -> Explained:
    basis = _basis(inputs, ("gross_profit", "revenue"))
    if isinstance(basis, Absent):
        return basis
    if basis.values["revenue"] <= 0:
        return Absent("revenue ≤ 0")
    return Ratio(
        basis.values["gross_profit"] / basis.values["revenue"], basis.kind, basis.period_end
    )


def _debt_to_equity(inputs: _Inputs, cap: Decimal | Absent) -> Explained:
    # The newest date both are held, within 456 days (plan amendment A4). A
    # negative equity would rank a heavily indebted company as the least
    # leveraged, so it is absent instead.
    found = _newest(inputs, ("total_debt", "stockholders_equity"))
    if isinstance(found, Absent):
        return found
    day, balances = found
    if balances["stockholders_equity"] <= 0:
        return Absent("equity ≤ 0")
    return Ratio(balances["total_debt"] / balances["stockholders_equity"], None, day)


def _interest_cover(inputs: _Inputs, cap: Decimal | Absent) -> Explained:
    # A debt-free company loses this metric, and the signal survives in the
    # pillar only while Yahoo still reports total_debt as 0 -- once it stops
    # publishing the series, debt_to_equity is absent too rather than zero.
    basis = _basis(inputs, ("ebit", "interest_expense"))
    if isinstance(basis, Absent):
        return basis
    if basis.values["interest_expense"] <= 0:
        return Absent("interest expense ≤ 0")
    return Ratio(
        basis.values["ebit"] / basis.values["interest_expense"], basis.kind, basis.period_end
    )


_FORMULAS: dict[str, Callable[[_Inputs, Decimal | Absent], Explained]] = {
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


def explain_ratios(
    held: Held,
    *,
    industry: str | None,
    close: Decimal | None,
    close_date: date | None,
    split_dates: Sequence[date],
    as_of: date,
    currency: str | None = None,
    foreign: Mapping[str, frozenset[str]] | None = None,
) -> dict[str, Explained]:
    """Every applicable ratio, as its value or the reason it has none (ui-swap D12).

    `currency` and `foreign` come from `index_facts_explained`; without them an
    absence caused by a dropped currency reads as a missing figure, which is all
    scoring needs and all `compute_ratios` passes.
    """
    wanted = {code for codes in applicable(industry).values() for code in codes}
    cap = explain_market_cap(
        held, close=close, close_date=close_date, split_dates=split_dates, as_of=as_of
    )
    inputs = _Inputs(held, as_of, currency, foreign or {})
    return {code: _FORMULAS[code](inputs, cap) for code in RATIO_CODES if code in wanted}


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
    explained = explain_ratios(
        held,
        industry=industry,
        close=close,
        close_date=close_date,
        split_dates=split_dates,
        as_of=as_of,
    )
    return {code: value for code, value in explained.items() if isinstance(value, Ratio)}
