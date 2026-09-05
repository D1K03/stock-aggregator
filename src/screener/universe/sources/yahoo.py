"""Company profile from Yahoo, called directly.

`quoteSummary` needs a crumb; `assetProfile` alone does not carry exchange or
currency, so `price` is requested alongside it. Both come back in one request.

No yfinance: it emits parsed DataFrames, and a payload stored from it would be
the library's reshaping rather than the response — which would break the
restatement detector and leave "did the API lie or did our parser?" unanswerable.

**This is the one module that does not go through `screener.fetch`.** Two reasons,
both structural rather than convenient. A crumb is only valid alongside the cookie
issued with it, and `fetch()` builds a fresh `httpx.Client` per call, so the jar is
discarded before the crumb it authorises is ever used — every request would 401.
And `fetch()` raises on a non-2xx, but here a 401 is information rather than a
failure: it is how Yahoo says the crumb expired, and the status is needed intact.
So this client owns one session for the whole run, which is also what the
throughput measurement in the spec was taken against.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

# Two refreshable failures (one 401, one 429) plus the retry each earns, then stop.
_MAX_ATTEMPTS = 4
TIMEOUT = 25.0
BASE = "https://query1.finance.yahoo.com"
COOKIE_URL = "https://fc.yahoo.com"
CRUMB_URL = f"{BASE}/v1/test/getcrumb"
MODULES = "assetProfile,price"

BROWSER = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

MIC_BY_EXCHANGE: dict[str, str] = {
    "NMS": "XNAS",
    "NGM": "XNAS",
    "NCM": "XNAS",
    "NYQ": "XNYS",
    "PCX": "ARCX",
    "ASE": "XASE",
    "BTS": "BATS",
}


class CrumbUnavailable(RuntimeError):
    """The handshake did not yield a crumb.

    Fatal for the run rather than for one symbol: without a crumb every
    `quoteSummary` call answers 401, so continuing would issue three thousand
    doomed requests and then write an empty CSV.
    """


@dataclass(frozen=True)
class Profile:
    sector: str
    industry: str
    mic: str
    currency: str


class YahooClient:
    """Holds the session and the crumb across symbols, refreshing when Yahoo expires it.

    One `httpx.Client` for the life of the run. That is not connection-pooling
    tidiness: the cookie set by the handshake has to travel with every later
    request, and a client rebuilt per call drops it.
    """

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] | None = None,
        backoff: float = 1.0,
    ) -> None:
        self._client = httpx.Client(
            transport=transport,
            follow_redirects=True,
            timeout=TIMEOUT,
            headers=BROWSER,
        )
        self._crumb: str | None = None
        self._sleep = sleep or time.sleep
        self._backoff = backoff
        self.crumb_fetches = 0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "YahooClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _request(self, url: str) -> httpx.Response | None:
        """One request on the shared session, returning the response whatever its status.

        A 401 is information here, not an error, so nothing raises on status.
        A transport-level failure is a genuine one and becomes None.
        """
        try:
            return self._client.get(url)
        except httpx.HTTPError:
            return None

    def _fetch_crumb(self) -> str:
        # fc.yahoo.com answers 404 while still setting the cookie, so its status
        # is deliberately ignored — the Set-Cookie header is the whole point.
        self._request(COOKIE_URL)
        response = self._request(CRUMB_URL)
        if response is None or response.status_code != 200 or not response.text.strip():
            raise CrumbUnavailable(
                "Yahoo would not issue a crumb; every quoteSummary call would 401"
            )
        crumb = response.text.strip()
        self._crumb = crumb
        self.crumb_fetches += 1
        return crumb

    def _crumbed(self) -> str:
        return self._crumb or self._fetch_crumb()

    def profile(self, symbol: str) -> Profile | None:
        """Sector, industry, MIC and currency, or None if Yahoo will not say.

        A 401 means the crumb expired, which over a long run is ordinary rather
        than exceptional: refresh once and retry. A 429 means we are going too
        fast, so back off and retry. Either way a second consecutive failure of
        the same kind stops here — retrying forever would hang the refresh, and
        the departure ceiling in `load` catches a CSV that came back mostly empty.
        """
        backoff = self._backoff
        refreshed = False
        for _ in range(_MAX_ATTEMPTS):
            crumb = self._crumbed()
            raw = self._request(
                f"{BASE}/v10/finance/quoteSummary/{symbol}?modules={MODULES}&crumb={crumb}"
            )
            if raw is None:
                return None
            if raw.status_code == 401 and not refreshed:
                self._crumb = None
                refreshed = True
                continue
            if raw.status_code == 429:
                self._sleep(backoff)
                backoff *= 2
                continue
            if raw.status_code != 200:
                return None
            return _parse(raw.text)
        return None


def _parse(text: str) -> Profile | None:
    import json

    try:
        result = json.loads(text)["quoteSummary"]["result"]
    except (KeyError, TypeError, ValueError):
        return None
    if not result:
        return None
    profile = result[0].get("assetProfile") or {}
    price = result[0].get("price") or {}
    sector = (profile.get("sector") or "").strip()
    industry = (profile.get("industry") or "").strip()
    if not sector:
        return None
    exchange = (price.get("exchange") or "").strip()
    return Profile(
        sector=sector,
        industry=industry,
        mic=MIC_BY_EXCHANGE.get(exchange, exchange),
        currency=(price.get("currency") or "USD").strip(),
    )
