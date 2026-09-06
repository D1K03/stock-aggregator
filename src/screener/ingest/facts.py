"""Timeseries JSON to facts. No I/O, no database, no clock.

Kept pure so the awkward cases -- nulls padding an array, a series we did not
ask for, a fiscal Q4 sharing its date with the fiscal year -- are tested
without a socket.

`Decimal` throughout rather than float, as `parse.py` does: a fundamental is
money and a float that reads 416160999999.99994 is a number nobody reported.
"""

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

# Yahoo's stem -> our `metric.code`, agreeing by hand with
# `migrations/021_fundamental_metrics.sql`. Both prefixes of each stem are
# requested, so this is 28 metrics and 56 `type=` values.
#
# What is absent is as deliberate as what is here: a line Yahoo *reports* is
# stored, a figure Yahoo *computes* from lines we already store is not. EBITDA
# (EBIT + depreciation) and free cash flow (operating cash flow less capital
# expenditure) were both measured as reproducing the reported figure exactly,
# so they are derived at scoring time rather than stored.
SERIES: dict[str, str] = {
    "TotalRevenue": "revenue",
    "CostOfRevenue": "cost_of_revenue",
    "GrossProfit": "gross_profit",
    "ResearchAndDevelopment": "research_and_development",
    "SellingGeneralAndAdministration": "selling_general_admin",
    "OperatingIncome": "operating_income",
    "EBIT": "ebit",
    "InterestExpense": "interest_expense",
    "PretaxIncome": "pretax_income",
    "TaxProvision": "tax_provision",
    "NetIncome": "net_income",
    "DepreciationAndAmortization": "depreciation_amortisation",
    "OperatingCashFlow": "operating_cash_flow",
    "CapitalExpenditure": "capital_expenditure",
    "TotalAssets": "total_assets",
    "CurrentAssets": "current_assets",
    "CurrentLiabilities": "current_liabilities",
    "TotalLiabilitiesNetMinorityInterest": "total_liabilities",
    "StockholdersEquity": "stockholders_equity",
    "CashAndCashEquivalents": "cash_and_equivalents",
    "CashCashEquivalentsAndShortTermInvestments": "cash_and_short_term_investments",
    "CurrentDebt": "current_debt",
    "LongTermDebt": "long_term_debt",
    "TotalDebt": "total_debt",
    "NetPPE": "net_ppe",
    "BasicAverageShares": "shares_basic_avg",
    "DilutedAverageShares": "shares_diluted_avg",
    "OrdinarySharesNumber": "shares_outstanding",
}

_PREFIX = {"annual": "A", "quarterly": "Q"}


@dataclass(frozen=True)
class Fact:
    metric_code: str
    period_end: date
    period_type: str
    value: Decimal
    currency: str | None


def _split(key: str) -> tuple[str, str] | None:
    """`annualTotalRevenue` -> ('revenue', 'A'), or None if not ours."""
    for prefix, period_type in _PREFIX.items():
        if key.startswith(prefix):
            code = SERIES.get(key[len(prefix):])
            return (code, period_type) if code else None
    return None


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        # str() first: Decimal(float) preserves the binary error rather than
        # the number the provider meant.
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    # Python's json decoder accepts bare `NaN` and `Infinity`. `numeric` would
    # take a NaN and then every comparison against it is false, which makes a
    # fact that can never be restated because it never equals itself.
    return number if number.is_finite() else None


def parse(payload: bytes) -> list[Fact]:
    """Every fact in one timeseries response, in the order Yahoo gave them."""
    try:
        body = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return []
    results = (body.get("timeseries") or {}).get("result") or []

    out: list[Fact] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        for key, entries in result.items():
            if key in ("meta", "timestamp") or not isinstance(entries, list):
                continue
            split = _split(key)
            if split is None:
                continue
            code, period_type = split
            for entry in entries:
                # Yahoo pads its arrays with nulls. A null becoming 0 would be
                # a fabricated fundamental, and `value` is not null precisely
                # so a missing number cannot be mistaken for a real one.
                if not isinstance(entry, dict):
                    continue
                value = _decimal((entry.get("reportedValue") or {}).get("raw"))
                as_of = entry.get("asOfDate")
                if value is None or not as_of:
                    continue
                try:
                    period_end = date.fromisoformat(as_of)
                except ValueError:
                    continue
                out.append(
                    Fact(code, period_end, period_type, value,
                         entry.get("currencyCode"))
                )
    return out
