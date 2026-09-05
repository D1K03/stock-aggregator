"""Daily bars from Yahoo, over a lane.

`/v8/finance/chart` needs no crumb — only a User-Agent — so this shares nothing
with `screener.universe.sources.yahoo` but `LanePool`. That is deliberate:
promoting the universe client would share the cookie and crumb machinery this
does not use. When fundamentals land and need the crumbed path, that is the
moment the promotion earns itself.
"""

import time
from collections.abc import Callable
from datetime import date, datetime, time as clock, timedelta, timezone
from urllib.parse import quote

import httpx

from screener.fetch import Lane, LanePool

BASE = "https://query1.finance.yahoo.com/v8/finance/chart"
TIMEOUT = 25.0
_SPARE_ATTEMPTS = 3

BROWSER = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def _epoch(day: date) -> int:
    return int(datetime.combine(day, clock.min, tzinfo=timezone.utc).timestamp())


class ChartClient:
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

    def __enter__(self) -> "ChartClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _request(self, lane: Lane, url: str) -> httpx.Response | None:
        try:
            return lane.get(url)
        except httpx.HTTPError:
            return None

    def fetch(self, symbol: str, start: date, end: date) -> bytes | None:
        """Raw response bytes for one symbol's window, or None.

        Bytes rather than parsed JSON because the caller hashes and stores them:
        parsing first and re-serialising would hash our reshaping of the response
        rather than the response, which is the mistake that rules out yfinance.

        None means "this security failed tonight" and never ends the run — D4 of
        the spec widens its window tomorrow.
        """
        # `period2` is the day *after* `end`, not `end` itself. `_epoch` is
        # midnight UTC and a US daily bar carries its session-open timestamp
        # (13:30/14:30 UTC), so an exclusive bound at `end 00:00Z` drops the
        # session that just closed — a run at 22:00 UTC would miss today while
        # the same run at 02:00 UTC would catch it. Coverage must not depend on
        # the hour cron fires.
        #
        # The symbol is escaped because it lands in the URL *path*: BRK.B is
        # harmless but a symbol carrying a space or a slash builds a URL httpx
        # rejects with `InvalidURL`, which is not an `HTTPError` and so would
        # escape `_request` and end the night.
        url = (
            f"{BASE}/{quote(symbol, safe='')}"
            f"?period1={_epoch(start)}&period2={_epoch(end + timedelta(days=1))}"
            "&interval=1d&events=div,split"
        )
        backoff = self._backoff
        lane = self.lanes.acquire()
        for _ in range(len(self.lanes) + _SPARE_ATTEMPTS):
            if lane.parked_for:
                # Every lane is cooling down. The wait lives here, not in the
                # pool: how long to give a source is the source's business.
                self._sleep(backoff)
                backoff *= 2
            response = self._request(lane, url)
            if response is None:
                return None
            if response.status_code == 429:
                lane.park(backoff)
                lane = self.lanes.acquire()
                continue
            if response.status_code != 200:
                return None
            return response.content
        return None
