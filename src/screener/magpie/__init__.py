"""Magpie: fetch an article over the cheapest route that works, and keep it.

    from screener.magpie import scrape
    outcome = scrape("https://example.com/an-article")

**The escalating ladder is not here.** `screener.fetch` already had it —
`direct -> isp_proxy -> unlocker`, escalating on any failure or an empty 2xx,
recording which rung answered — and nothing in the project had ever named the
billed rung. What this package adds is the policy around it: whether a site
permits the request at all, what counts as a page rather than a block, what a
page costs, and where the result is kept.

Three refusals deliberately do **not** escalate, which is the opposite of the
rule everywhere else here:

- **robots.txt said no.** Reaching for a residential proxy to fetch a page a
  site asked us not to would turn "respect robots.txt" into "route around
  robots.txt", which is worse than not having the rule.
- **It is not a web page.** `FetchResult` carries text and no Content-Type, so
  a PDF arrives as mojibake rather than as an error; without this the ladder
  climbs to the billed rung to fail again. Paying to fail is worth code to
  avoid.
- **The publisher says it is not free to read.** "The same bytes a browser
  gets" is the justification `screener.fetch` writes down for sending a browser
  User-Agent. Paying a proxy network to get past a subscription check is a
  different act.

`extract` is deliberately not re-exported: reaching it is one import away from
lxml, and the two processes that only want a document — the status service and
the bot — should not pay for a parser to ask for one. Reach into
`screener.magpie.extract` if you are the scraper, and nothing else should be.
"""

from screener.magpie.config import MagpieConfig
from screener.magpie.models import Document, Extracted, Refused
from screener.magpie.run import OPERATION, Outcome, scrape
from screener.magpie.urls import canonical, host_of, is_http, looks_binary

__all__ = [
    "OPERATION",
    "Document",
    "Extracted",
    "MagpieConfig",
    "Outcome",
    "Refused",
    "canonical",
    "host_of",
    "is_http",
    "looks_binary",
    "scrape",
]
