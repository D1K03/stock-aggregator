"""Ticker to CIK, from SEC's published filer list.

CIK is the identity anchor the reconciler matches on. A symbol is a mutable
attribute — match on it and a rename reads as a departure plus an unrelated
arrival, orphaning the company's history behind a dead ticker.
"""

import httpx

from screener.config import env
from screener.fetch import fetch
from screener.universe.rows import normalise_symbol

SEC_URL = "https://www.sec.gov/files/company_tickers.json"

# What SEC actually enforces, measured against the live endpoint rather than
# inferred from its policy page:
#
#   User-Agent                              response
#   ------------------------------------    --------
#   (none)                                  403
#   a browser UA                            403
#   screener/0.1 (+https://github.com/…)    403
#   d1k03@github.com                        403
#   stock-aggregator/0.1 (a@example.com)    200
#
# So a contact address is necessary, and the string `github.com` is
# disqualifying on its own — an address at that domain is refused as firmly as
# a bare repository URL. The original header here carried a GitHub URL and no
# address, which is why it had never once succeeded: every test covering it
# used a MockTransport, so the 403 only ever appeared against the real thing.
BANNED = "github.com"


def user_agent() -> str:
    """The header SEC requires, built from the deployer's contact address.

    Read from the environment rather than hardcoded because it identifies
    *whoever is running this* to a government service, which is a per-deployer
    fact and not a property of the code. Unset is fatal rather than defaulted:
    a placeholder address would be answered with a 403 that reads as a network
    fault, and this failure has already cost one debugging session.
    """
    email = env.optional("SEC_CONTACT_EMAIL")
    if not email:
        raise RuntimeError(
            "SEC_CONTACT_EMAIL is not set. SEC answers 403 to any request "
            "whose User-Agent carries no contact address, so the universe "
            "refresh cannot run without one."
        )
    if BANNED in email.lower():
        raise RuntimeError(
            f"SEC_CONTACT_EMAIL must not contain {BANNED!r}: SEC refuses those "
            "with a 403 regardless of whether the address is well formed."
        )
    return f"stock-aggregator/0.1 ({email})"


def cik_by_symbol(*, transport: httpx.BaseTransport | None = None) -> dict[str, str]:
    """Every US filer's ticker mapped to its zero-padded 10-digit CIK."""
    result = fetch(
        SEC_URL, headers={"User-Agent": user_agent()}, transport=transport
    )
    payload = result.json()
    out: dict[str, str] = {}
    for entry in payload.values():
        ticker = entry.get("ticker")
        cik = entry.get("cik_str")
        if ticker and cik is not None:
            out[normalise_symbol(str(ticker))] = f"{int(cik):010d}"
    return out
