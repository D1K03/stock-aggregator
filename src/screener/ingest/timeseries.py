"""Fundamentals from Yahoo's timeseries endpoint, over a lane.

Measured before this was written: a cold `httpx.Client` with no cookie jar and
no crumb parameter gets 200. So this is `ChartClient`'s shape rather than
`screener.universe.sources.yahoo`'s -- the cookie-and-crumb apparatus exists
because a crumb is only valid alongside the cookie issued with it, and an
endpoint that needs neither should not pay for machinery serving one that does.

`PLAN.md` says the Yahoo path holds one session for the run. That is true of
`quoteSummary` and false here, and the spec's F4 records the measurement.
"""

import time
from collections.abc import Callable
from urllib.parse import quote

import httpx

from screener.fetch import Lane, LanePool
from screener.ingest.facts import SERIES

BASE = "https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries"
TIMEOUT = 40.0
_SPARE_ATTEMPTS = 3

# Far enough back to take everything Yahoo holds. It returns four annual and
# five quarterly periods whatever is asked for, so this is a formality until
# the shape of the response changes.
PERIOD1 = 1420070400  # 2015-01-01
PERIOD2 = 4102444800  # 2100-01-01

BROWSER = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# Both prefixes of every stem, in one request: 28 metrics, 56 values.
TYPES = ",".join(
    f"{prefix}{stem}" for stem in SERIES for prefix in ("annual", "quarterly")
)


class TimeseriesClient:
    def __init__(
        self,
        *,
        lanes: LanePool | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] | None = None,
        backoff: float = 1.0,
    ) -> None:
        self._owned = lanes is None
        self.lanes = lanes or LanePool.from_env(
            headers=BROWSER,
            timeout=TIMEOUT,
            transport=transport,
            fallback_to_direct=True,
        )
        self._sleep = sleep or time.sleep
        self._backoff = backoff

    def close(self) -> None:
        if self._owned:
            self.lanes.close()

    def __enter__(self) -> "TimeseriesClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _request(self, lane: Lane, url: str) -> httpx.Response | None:
        try:
            return lane.get(url)
        except httpx.HTTPError:
            return None

    def fetch(self, symbol: str) -> bytes | None:
        """One security's fundamentals, or None if this security failed."""
        # The symbol lands in the URL *path*, so it is escaped: a symbol
        # carrying a slash builds a URL httpx rejects with `InvalidURL`, which
        # descends from Exception rather than HTTPError and so escapes the
        # guard in `_request` entirely.
        safe = quote(symbol, safe="")
        url = (
            f"{BASE}/{safe}?symbol={safe}&type={TYPES}"
            f"&period1={PERIOD1}&period2={PERIOD2}"
        )
        backoff = self._backoff
        for _ in range(len(self.lanes) + _SPARE_ATTEMPTS):
            lane = self.lanes.acquire()
            if lane.parked_for:
                # Every lane is on cooldown. How long to give a source is the
                # source's business, which is why the wait is here and not in
                # the fetch layer.
                self._sleep(backoff)
                backoff *= 2
            raw = self._request(lane, url)
            if raw is None:
                return None
            if raw.status_code == 429:
                lane.park(backoff)
                backoff *= 2
                continue
            if raw.status_code != 200:
                return None
            return raw.content
        return None
