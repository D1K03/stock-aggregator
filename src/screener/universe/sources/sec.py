"""Ticker to CIK, from SEC's published filer list.

CIK is the identity anchor the reconciler matches on. A symbol is a mutable
attribute — match on it and a rename reads as a departure plus an unrelated
arrival, orphaning the company's history behind a dead ticker.
"""

import httpx

from screener.fetch import fetch
from screener.universe.rows import normalise_symbol

SEC_URL = "https://www.sec.gov/files/company_tickers.json"

# SEC returns 403 to requests without a declared User-Agent carrying contact
# details. This is enforced, not advisory.
SEC_HEADERS = {
    "User-Agent": "screener/0.1 (universe refresh; +https://github.com/D1K03/stock-aggregator)"
}


def cik_by_symbol(*, transport: httpx.BaseTransport | None = None) -> dict[str, str]:
    """Every US filer's ticker mapped to its zero-padded 10-digit CIK."""
    result = fetch(SEC_URL, headers=SEC_HEADERS, transport=transport)
    payload = result.json()
    out: dict[str, str] = {}
    for entry in payload.values():
        ticker = entry.get("ticker")
        cik = entry.get("cik_str")
        if ticker and cik is not None:
            out[normalise_symbol(str(ticker))] = f"{int(cik):010d}"
    return out
