"""Insider transactions: SEC Form 4, for the Insider/Institutional pillar.

Two halves that share nothing but a dataclass. `source` talks to the network and
never opens a database connection; `store` writes rows and never opens a socket.
That is the split `screener.universe` draws between `refresh` and `load` and
`screener.reddit` draws between its own two, and it is what lets either half be
tested without the other.

**An API before a scraper, and this one is unambiguous.** EDGAR needs no key, no
session and no proxy; `sec.gov/robots.txt` does not disallow `/Archives/`; and
SEC publishes both the format and the rate it will serve. What it does require
is a User-Agent naming a contact address, which is why `EDGAR_CONTACT_EMAIL` is
at once the credential and the off switch — there is nothing here to turn on
without one.

Where `screener.reddit` and `screener.magpie` both stop short of connecting a
text to a security, this does not have to: a Form 4 names its issuer's CIK, and
`screener.universe` already matches identity on CIK rather than on symbol. The
hard problem those two ran into does not arise here, which is most of why this
is the pillar input that could be built first.

Ingest only, all the same. Nothing here scores anything — wiring it into the
Insider pillar needs a weight-version bump and a backfill with alerting
disabled, per `DESIGN.md`, not a table.
"""

from screener.edgar.config import EdgarConfig
from screener.edgar.ingest import Report, once
from screener.edgar.source import Owner, SourceError, Throttled, Transaction

__all__ = [
    "EdgarConfig", "Owner", "Report", "SourceError", "Throttled",
    "Transaction", "once",
]
