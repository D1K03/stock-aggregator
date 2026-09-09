"""Getting one page, over the cheapest route that works.

The ladder itself is `screener.fetch`, which already escalates on any failure or
an empty 2xx and records which rung answered. What lives here is the policy
around it: whether the site permits this at all, what counts as a page rather
than a block, and what it cost.

Three cases here deliberately do **not** escalate, and they are the interesting
part — everywhere else in this project the answer to a failure is "try the next
strategy".

- **robots.txt said no.** Escalating would turn "respect robots.txt" into
  "route around robots.txt" using a residential proxy, which is worse than not
  having the rule.
- **The page is not a web page.** `FetchResult` carries text and no
  Content-Type, so a PDF arrives as mojibake rather than an error; the extractor
  finds nothing and the ladder would climb all the way to the billed rung to
  fail again. Paying to fail is the one outcome worth spending code to avoid.
- **The publisher says it is not free to read.** Getting the same bytes a
  browser gets is the justification `screener.fetch` writes down for sending a
  browser User-Agent. Paying a proxy network to get past a subscription check is
  a different act, and not one this should make by default.
- **The address is not on the public internet.** See `screener.magpie.reachable`:
  the destination here is somebody else's input, and a scraper that will fetch
  `169.254.169.254` and show you the answer is a window into the private network
  rather than a reader of the web.

It also does not sleep or retry inside the chain: D6 of the infrastructure spec
keeps that out of the fetch layer, and the per-host floor below is the caller's
own, which is where D6 puts it.
"""

import logging
import re
from dataclasses import dataclass
from decimal import Decimal

import httpx

from screener.fetch import BROWSER_HEADERS, FetchResult, fetch
from screener.fetch.result import FetchError
from screener.fetch.strategies import OnRequest
from screener.magpie import reachable, robots
from screener.magpie.config import UNLOCKER_USD, MagpieConfig
from screener.magpie.urls import is_http, looks_binary

logger = logging.getLogger(__name__)


class Blocked(RuntimeError):
    """A 2xx that is not the page: a challenge, a consent wall, a login."""


class NotPermitted(RuntimeError):
    """robots.txt says no. Terminal, and never escalated past."""


class NotAPage(RuntimeError):
    """Not HTML, or not free to read. Terminal, so the ladder cannot pay to fail."""


# Markers that a 200 is a bot check rather than an article. Matched against the
# opening of the body, because a real article can perfectly well use these words
# further down — a piece *about* Cloudflare is not a Cloudflare challenge.
_CHALLENGE = re.compile(
    r"(just a moment|checking your browser|enable javascript|verify you are (a )?human"
    r"|attention required|access denied|are you a robot|captcha)",
    re.IGNORECASE,
)
_CHALLENGE_WINDOW = 2000

# Under this, a 200 carries no page. Some providers answer a rate limit with a
# near-empty body rather than a 429, and `screener.fetch` already treats a truly
# empty one as a failure; this catches the almost-empty case.
_MIN_BYTES = 500

# schema.org's machine-readable "this is behind a paywall", which publishers
# emit far more reliably than they make the text reachable.
_PAYWALLED = re.compile(r'"isAccessibleForFree"\s*:\s*(false|"False")', re.IGNORECASE)

# Enough of the body to tell markup from a binary blob rendered as text.
_SNIFF = 1024


@dataclass(frozen=True, slots=True)
class Fetched:
    """One page, and what getting it took."""

    html: str
    url: str
    strategy: str
    attempts: tuple[str, ...]
    status_code: int

    @property
    def cost_usd(self) -> Decimal:
        """What this fetch is billed. Zero unless it reached the paid rung.

        `isp_proxy` is bandwidth against a flat monthly plan, so its marginal
        cost really is zero rather than merely small.
        """
        return UNLOCKER_USD if self.strategy == "unlocker" else Decimal(0)


def _looks_like_a_page(result: FetchResult) -> None:
    """Reject a 200 that is not the article, so the chain escalates.

    This is the `validate` hook `screener.fetch` offers, and it is what makes a
    *soft* block escalate. A challenge page is served with a 200 and a body, so
    without this the chain would stop at the first rung, store an interstitial
    as an article, and never try the proxy that would have worked.
    """
    if len(result.text) < _MIN_BYTES:
        raise Blocked(f"{len(result.text)} bytes is not a page")
    if _CHALLENGE.search(result.text[:_CHALLENGE_WINDOW]):
        raise Blocked("the response is a bot check, not the page")


def ladder(config: MagpieConfig, *, may_pay: bool) -> tuple[str, ...]:
    """The strategies this fetch may use.

    `may_pay` is the daily meter's answer, and dropping the billed rung is how a
    refusal here stays cheap rather than total: a capped scrape still tries both
    free strategies, so the meter can fail closed without refusing anybody.
    """
    if may_pay:
        return config.strategies
    return tuple(name for name in config.strategies if name != "unlocker") or ("direct",)


def acquire(
    url: str,
    config: MagpieConfig | None = None,
    *,
    may_pay: bool = True,
    transport: httpx.BaseTransport | None = None,
    on_request: OnRequest = reachable.guard,
) -> Fetched:
    """Fetch one URL over the configured ladder.

    Raises `NotPermitted` when robots.txt refuses and `NotAPage` when the
    address or the body is not a readable page — both before anything is paid
    for — and `FetchError` when every rung it was allowed failed.
    """
    config = config or MagpieConfig.from_env()

    if not is_http(url):
        raise NotAPage(f"not an http address: {url[:80]}")
    if looks_binary(url):
        # Refused by path, before a request. Cheaper than discovering it from a
        # body that has no Content-Type to check.
        raise NotAPage("that address is a file, not a page")

    # Checked here as well as on every hop, so a private address submitted
    # directly is refused without a request being made at all. The hook is what
    # actually holds the line, because it is the only thing that sees a
    # redirect; this only saves a pointless request.
    #
    # Both are the same switch. A caller passing no hook has said it does not
    # want addresses checked, and a test exercising robots parsing behind a
    # MockTransport is the only caller that ever does — resolving names it never
    # dials would make the suite depend on DNS.
    if on_request is not None:
        try:
            reachable.check(url)
        except reachable.NotReachable as exc:
            raise NotPermitted(str(exc)) from exc

    permission = robots.rules(
        url, user_agent=config.user_agent, transport=transport, on_request=on_request
    )
    if not permission.allowed:
        # Deliberately not a FetchError: nothing failed, and nothing should be
        # retried down a more expensive path.
        raise NotPermitted(f"robots.txt disallows {url}")

    headers = {**BROWSER_HEADERS, "User-Agent": config.user_agent}
    result = fetch(
        url,
        ladder(config, may_pay=may_pay),
        timeout=config.timeout,
        headers=headers,
        validate=_looks_like_a_page,
        transport=transport,
        # Every request, including each redirect followed. Validating only the
        # URL above would check an address nobody fetches: a public page can
        # answer `302 Location: http://169.254.169.254/` and the request goes
        # there with nothing having looked.
        on_request=on_request,
    )

    opening = result.text[:_SNIFF].lstrip().lower()
    if not (opening.startswith("<!doctype") or "<html" in opening or "<head" in opening):
        raise NotAPage("the response is not HTML")
    if _PAYWALLED.search(result.text):
        raise NotAPage("the publisher marks this as not free to read")

    if result.strategy != config.strategies[0]:
        logger.info("magpie fell back to %s after %s", result.strategy, result.attempts[:-1])

    return Fetched(
        html=result.text,
        url=result.url,
        strategy=result.strategy,
        attempts=result.attempts,
        status_code=result.status_code,
    )


__all__ = ["Blocked", "Fetched", "NotAPage", "NotPermitted", "acquire", "ladder", "FetchError"]
