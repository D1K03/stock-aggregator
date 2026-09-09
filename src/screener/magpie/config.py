"""Magpie's own configuration.

Owned here rather than in `screener.config`, like every other subsystem: a
process that only ever asks for a document should not need a database URL, and
a process that never scrapes should not carry these defaults.
"""

import os
from dataclasses import dataclass
from decimal import Decimal

# The order tried, cheapest first. `direct` is free; `isp_proxy` is four exit
# addresses on a flat monthly plan already paid for, so its marginal cost is
# zero; `unlocker` is roughly $0.002 per successful request and is the only rung
# that spends anything. Configuration rather than a constant because the whole
# point of the registry is that rungs can be inserted ahead of the paid one.
DEFAULT_STRATEGIES: tuple[str, ...] = ("direct", "isp_proxy", "unlocker")

# What Bright Data bills for one successful Web Unlocker request. A local price
# table is wrong the moment it changes, which is why model spend is read from
# the provider's own response instead — but the Unlocker returns a page, not an
# invoice, so this is the honest option and it is written down where it can be
# corrected rather than buried in a call site.
UNLOCKER_USD = Decimal("0.002")

# Identifies the scraper to a site, and is the agent robots.txt is evaluated
# against. A real contact address, because a host that wants to complain should
# be able to.
DEFAULT_USER_AGENT = "MagpieBot/1.0 (+https://github.com/D1K03/stock-aggregator)"

# A page larger than this is not an article worth keeping. Also the ceiling on
# what one request can spend on bandwidth billed by the gigabyte.
MAX_BYTES = 4_000_000

DEFAULT_TIMEOUT = 30.0

# How many billed fetches a day, across everyone.
#
# **Not `DAILY_SPEND_CAP_USD`.** Putting this on the chat meter was the first
# design and it was wrong: that cap defaults to $0.10 and a reply costs about
# $0.00005, so fifty scrapes would exhaust the entire daily allowance for both
# people and the symptom would be Steven going quiet — a scraping feature
# silently starving the assistant. The two things differ by a factor of forty
# and one counter cannot mean both.
#
# The cost is still recorded on the audit row, because that is where money
# belongs and `/audit` should show it. This is the gate; that is the ledger.
DEFAULT_UNLOCKER_DAILY_MAX = 20

# The floor between two requests to one host, unless its robots.txt asks for
# longer. One person pasting one link never meets this; it exists so the crawler
# does not have to retrofit politeness into a path that never had any.
DEFAULT_HOST_DELAY = 1.0


@dataclass(frozen=True, slots=True)
class MagpieConfig:
    """Where magpie is, and what it may do to get a page."""

    url: str = "http://magpie:8082"
    strategies: tuple[str, ...] = DEFAULT_STRATEGIES
    user_agent: str = DEFAULT_USER_AGENT
    timeout: float = DEFAULT_TIMEOUT
    unlocker_daily_max: int = DEFAULT_UNLOCKER_DAILY_MAX
    host_delay: float = DEFAULT_HOST_DELAY

    @classmethod
    def from_env(cls) -> "MagpieConfig":
        raw = (os.environ.get("MAGPIE_STRATEGIES") or "").strip()
        strategies = tuple(s.strip() for s in raw.split(",") if s.strip()) or DEFAULT_STRATEGIES
        return cls(
            url=(os.environ.get("MAGPIE_URL") or "http://magpie:8082").rstrip("/"),
            strategies=strategies,
            user_agent=os.environ.get("MAGPIE_USER_AGENT") or DEFAULT_USER_AGENT,
            timeout=float(os.environ.get("MAGPIE_TIMEOUT") or DEFAULT_TIMEOUT),
            unlocker_daily_max=int(
                os.environ.get("MAGPIE_UNLOCKER_DAILY_MAX") or DEFAULT_UNLOCKER_DAILY_MAX
            ),
            host_delay=float(os.environ.get("MAGPIE_HOST_DELAY") or DEFAULT_HOST_DELAY),
        )

    @property
    def may_pay(self) -> bool:
        """Whether the configured ladder is allowed to reach the billed rung."""
        return "unlocker" in self.strategies
