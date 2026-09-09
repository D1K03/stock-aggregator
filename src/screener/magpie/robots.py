"""robots.txt, honoured rather than consulted.

`CLAUDE.md` says scrapers respect robots.txt and ToS, and until now that was a
policy applied by a person when choosing a source — it is why `screener.reddit`
reads a mirror rather than reddit.com. This is the first code that enforces it.

**A refusal here is terminal.** It never falls through to the next fetch
strategy, because escalating past a `Disallow` would turn "respect robots.txt"
into "route around robots.txt" — using a residential proxy to fetch a page the
site asked us not to is worse than not having the rule at all.

stdlib `urllib.robotparser` rather than `protego`. It gives `can_fetch` and
`crawl_delay` and costs no dependency; what it is weaker at is wildcard patterns,
which matters for a crawl over paths nobody has looked at and not for a URL a
person pasted. When crawling lands, `protego` is the upgrade.
"""

import logging
import threading
import time
import urllib.robotparser
from dataclasses import dataclass

import httpx
from urllib.parse import urlsplit

from screener.fetch import fetch
from screener.fetch.result import FetchError

logger = logging.getLogger(__name__)

# How long a host's robots.txt is trusted. Long enough that scraping several
# pages from one site is one fetch, short enough that a site that changes its
# mind is honoured the same day.
TTL_SECONDS = 3600.0

# robots.txt is small and a site that will not serve it quickly is a site whose
# rules we cannot read; both are answered the same way.
TIMEOUT = 10.0


@dataclass(frozen=True, slots=True)
class Rules:
    """One host's robots.txt, as far as we are concerned."""

    #: False only when the file was read and it said no. A host that has no
    #: robots.txt, or would not serve it, permits by default — which is what
    #: the standard says absence means.
    allowed: bool
    #: Seconds the site asks between requests, if it says.
    crawl_delay: float | None = None


class _Cache:
    """Parsed robots.txt per host.

    A lock because the extraction service answers requests on threads and two
    arriving together for one host would otherwise fetch it twice. The fetch
    happens outside the lock; only the dictionary is guarded.
    """

    def __init__(self) -> None:
        self._parsers: dict[str, tuple[float, urllib.robotparser.RobotFileParser | None]] = {}
        self._lock = threading.Lock()

    def get(
        self, origin: str, transport: httpx.BaseTransport | None = None
    ) -> urllib.robotparser.RobotFileParser | None:
        now = time.monotonic()
        with self._lock:
            found = self._parsers.get(origin)
            if found and now - found[0] < TTL_SECONDS:
                return found[1]

        parser = _read(origin, transport)

        with self._lock:
            self._parsers[origin] = (now, parser)
        return parser

    def clear(self) -> None:
        with self._lock:
            self._parsers.clear()


_CACHE = _Cache()


def _read(origin: str, transport: httpx.BaseTransport | None = None) -> urllib.robotparser.RobotFileParser | None:
    """Fetch and parse one host's robots.txt. `None` when there is nothing to obey.

    Direct only, deliberately. Reaching for a proxy to read the file that says
    whether we are welcome would be absurd, and a host that blocks the VPS from
    reading its robots.txt has not thereby permitted anything.
    """
    parser = urllib.robotparser.RobotFileParser()
    try:
        result = fetch(
            f"{origin}/robots.txt",
            ("direct",),
            timeout=TIMEOUT,
            allow_empty=True,
            transport=transport,
        )
    except FetchError as exc:
        # No robots.txt, or unreachable. The standard reads absence as
        # permission, and a 404 is by far the most common case.
        logger.info("no robots.txt for %s (%s)", origin, type(exc.__cause__ or exc).__name__)
        return None

    parser.parse(result.text.splitlines())
    return parser


def rules(
    url: str, *, user_agent: str, transport: httpx.BaseTransport | None = None
) -> Rules:
    """What this host permits us, for this URL.

    Never raises. A robots.txt that cannot be read or cannot be parsed permits,
    because the alternative — refusing everything on a transport error — makes a
    blip look like a policy.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return Rules(allowed=False)

    origin = f"{parts.scheme}://{parts.netloc}"
    try:
        parser = _CACHE.get(origin, transport)
    except Exception as exc:
        logger.warning("could not read robots.txt for %s: %s", origin, exc)
        return Rules(allowed=True)

    if parser is None:
        return Rules(allowed=True)

    try:
        allowed = parser.can_fetch(user_agent, url)
        delay = parser.crawl_delay(user_agent)
    except Exception as exc:
        logger.warning("could not apply robots.txt for %s: %s", origin, exc)
        return Rules(allowed=True)

    return Rules(allowed=allowed, crawl_delay=float(delay) if delay else None)


def forget() -> None:
    """Drop the cache. For tests, and for a host that has just been corrected."""
    _CACHE.clear()
