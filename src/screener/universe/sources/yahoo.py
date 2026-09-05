"""Company profile from Yahoo, called directly.

`quoteSummary` needs a crumb; `assetProfile` alone does not carry exchange or
currency, so `price` is requested alongside it. Both come back in one request.

No yfinance: it emits parsed DataFrames, and a payload stored from it would be
the library's reshaping rather than the response — which would break the
restatement detector and leave "did the API lie or did our parser?" unanswerable.

**This is the one module that does not go through `fetch()`.** Two reasons, both
structural rather than convenient. A crumb is only valid alongside the cookie
issued with it, and `fetch()` builds a fresh `httpx.Client` per call, so the jar is
discarded before the crumb it authorises is ever used — every request would 401.
And `fetch()` raises on a non-2xx, but here a 401 is information rather than a
failure: it is how Yahoo says the crumb expired, and the status is needed intact.

It does borrow one thing from `screener.fetch`, and nothing else: a `LanePool`,
which is the session `fetch()` cannot hold. With no Bright Data addresses
configured that is a single direct lane — byte for byte the client the throughput
measurement in the spec was taken against. Given several, each lane holds its own
jar and therefore its own crumb. That is deliberate rather than forced: measured
against the live zone, a crumb issued through one exit and replayed through
another still answers 200, so Yahoo binds a crumb to the jar and not to the
address. Keeping them separate anyway is worth it because four addresses sharing
one cookie is the pattern bot detection is built to notice.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from screener.fetch import Lane, LanePool

# Two refreshable failures (one 401, one 429) plus the retry each earns, and one
# turn per lane on top, so a pool of four can meet a 429 on every address and
# still have the refresh left. With a single lane this is the same 4 it was
# before lanes existed.
_SPARE_ATTEMPTS = 3
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

    Still fatal when there are several lanes and only one of them failed. A
    handshake that fails on a paid exit almost certainly means Yahoo is blocking
    that address, which `docs/infrastructure.md` says is the thing you want to
    notice — carrying on at three-quarter throughput would hide it.
    """


@dataclass(frozen=True)
class Profile:
    sector: str
    industry: str
    mic: str
    currency: str


class YahooClient:
    """Holds the sessions and their crumbs across symbols, refreshing when Yahoo expires them.

    One `httpx.Client` per lane, for the life of the run. That is not
    connection-pooling tidiness: the cookie set by the handshake has to travel
    with every later request down the same lane, and a client rebuilt per call
    drops it.
    """

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] | None = None,
        backoff: float = 1.0,
        lanes: LanePool | None = None,
    ) -> None:
        # Yahoo starts on the proxy rather than falling back to it once blocked:
        # a night's ingest is ~3,000 requests off one address, and a pinned lane
        # measured 1.07x direct latency, so spreading them costs about twenty
        # seconds. With no addresses configured this is one direct lane, which
        # is also the off switch — clear BRIGHTDATA_PROXY_IPS and it is gone.
        self._owned = lanes is None
        self._lanes = lanes or LanePool.from_env(
            headers=BROWSER,
            timeout=TIMEOUT,
            transport=transport,
            fallback_to_direct=True,
        )
        # A crumb belongs to the jar it was issued into, and each lane owns one.
        self._crumbs: dict[str, str] = {}
        self._sleep = sleep or time.sleep
        self._backoff = backoff
        self.crumb_fetches = 0

    def close(self) -> None:
        # Only a pool we opened is ours to close, as in `refresh`.
        if self._owned:
            self._lanes.close()

    def __enter__(self) -> "YahooClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _request(self, lane: Lane, url: str) -> httpx.Response | None:
        """One request on a lane's session, returning the response whatever its status.

        A 401 is information here, not an error, so nothing raises on status.
        A transport-level failure is a genuine one and becomes None.
        """
        try:
            return lane.get(url)
        except httpx.HTTPError:
            return None

    def _fetch_crumb(self, lane: Lane) -> str:
        # fc.yahoo.com answers 404 while still setting the cookie, so its status
        # is deliberately ignored — the Set-Cookie header is the whole point, and
        # it has to land in this lane's jar rather than any other.
        self._request(lane, COOKIE_URL)
        response = self._request(lane, CRUMB_URL)
        if response is None or response.status_code != 200 or not response.text.strip():
            raise CrumbUnavailable(
                f"Yahoo would not issue a crumb on {lane.name}; "
                "every quoteSummary call down it would 401"
            )
        crumb = response.text.strip()
        self._crumbs[lane.name] = crumb
        self.crumb_fetches += 1
        return crumb

    def _crumbed(self, lane: Lane) -> str:
        crumb = self._crumbs.get(lane.name)
        return crumb if crumb is not None else self._fetch_crumb(lane)

    def profile(self, symbol: str) -> Profile | None:
        """Sector, industry, MIC and currency, or None if Yahoo will not say.

        A 401 means this lane's crumb expired, which over a long run is ordinary
        rather than exceptional: refresh it and retry *on the same lane*, because
        rotating would hand the retry a different jar and prove nothing about the
        one that expired. A 429 means this exit is going too fast, so it is parked
        and the retry leaves by a different address — and with a single lane there
        is no different address, so it falls back to sleeping, which is what it has
        always done. Either way a second consecutive failure of the same kind stops
        here — retrying forever would hang the refresh, and the departure ceiling in
        `load` catches a CSV that came back mostly empty.
        """
        backoff = self._backoff
        refreshed = False
        lane = self._lanes.acquire()
        for _ in range(len(self._lanes) + _SPARE_ATTEMPTS):
            if lane.parked_for:
                # Every lane is on cooldown, so there is nowhere fresh to send
                # this. The wait is here rather than in the pool because how long
                # to give a source is the source's business, and D6 keeps that
                # out of the fetch layer.
                self._sleep(backoff)
                backoff *= 2
            crumb = self._crumbed(lane)
            raw = self._request(
                lane,
                f"{BASE}/v10/finance/quoteSummary/{symbol}?modules={MODULES}&crumb={crumb}",
            )
            if raw is None:
                return None
            if raw.status_code == 401 and not refreshed:
                self._crumbs.pop(lane.name, None)
                refreshed = True
                continue
            if raw.status_code == 429:
                lane.park(backoff)
                lane = self._lanes.acquire()
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
