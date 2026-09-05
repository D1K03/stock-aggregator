"""Long-lived sessions, each pinned to one Bright Data exit.

`fetch()` is stateless by construction: a fresh client per call, and the chain is
the retry. That is right for a request that stands alone, and wrong for Yahoo's
crumbed endpoint, where the cookie has to outlive the call that fetched it. A
lane is the session `fetch()` cannot hold — one client, one jar, one exit, for
the length of a run — and a pool hands lanes out round-robin so a night's work
leaves by every address the zone holds rather than piling onto one of them.

**One jar per lane is a robustness choice, not a correctness requirement.**
Measured against the live zone: a crumb and cookie issued through one exit and
replayed through another returns 200, so Yahoo binds a crumb to the jar and not
to the address. Keeping a jar per lane anyway is worth it because four addresses
sharing a single cookie is the exact pattern bot detection looks for, because
Yahoo's crumb scheme is undocumented and has changed before, and because an
`httpx.Client` owns its jar already — sharing one across four clients would mean
copying cookies by hand.

**Rotation is not concurrency.** A lane changes which address a request leaves
by, never how many are in flight. Callers stay sequential.

**This is not a rate limiter.** D6 in the infrastructure spec keeps throttling
out of the fetch layer and puts it with ingest, where a source's published limit
is actually known, and nothing here reverses that. A pool never sleeps, never
retries, and never issues a request a caller did not ask for. `park()` records
that a caller saw a 429 and how long it wants that exit left alone; `acquire()`
reads that record when choosing. Both the number and the waiting stay with the
caller.

The Web Unlocker cannot be a lane. It POSTs each URL to Bright Data's API as an
independent call, so there is no jar to keep, which is the one thing a lane is for.

The `transport` argument exists so tests can pass `httpx.MockTransport`, as in
`screener.fetch.strategies`. Production never passes one — and a test that wants
to prove two lanes do not share a jar passes a different transport to each.
"""

import logging
import time
from collections.abc import Callable, Mapping, Sequence

import httpx

from screener.fetch.chain import DEFAULT_TIMEOUT
from screener.fetch.config import ProxyConfig
from screener.fetch.result import StrategyUnavailable
from screener.fetch.strategies import BROWSER_HEADERS, redact

logger = logging.getLogger(__name__)

DIRECT_LANE = "direct"


class Lane:
    """One HTTP session that always leaves by the same address.

    Owns its `httpx.Client` outright and does not expose it. A caller holding the
    client could close it, rewrite its headers, or keep using it after the pool
    had shut down, and the two invariants this type exists to hold — one jar, one
    exit — would stop being its own to keep.
    """

    def __init__(
        self,
        name: str,
        *,
        proxy: str | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._name = name
        # `time.monotonic`, never `time.time`: a wall-clock jump would either
        # unpark every lane at once or strand them all, and neither reads as a
        # clock problem when you are looking at a stalled run.
        self._clock = clock or time.monotonic
        self._free_at = 0.0
        self._client = httpx.Client(
            # `transport` wins when supplied. httpx builds proxy *mounts*, and a
            # mount takes precedence over an explicit transport, so a
            # MockTransport under test would otherwise still try to dial out.
            proxy=None if transport is not None else proxy,
            # Only for a proxied lane. Bright Data terminates TLS on its own
            # chain, so the certificate presented is theirs rather than the
            # origin's and validating it against the origin hostname fails by
            # construction. A direct lane keeps verification on: turning it off
            # for the default path would be a real regression hidden inside a
            # proxy feature.
            verify=proxy is None,
            follow_redirects=True,
            timeout=timeout,
            headers=dict(headers if headers is not None else BROWSER_HEADERS),
            transport=transport,
        )

    @property
    def name(self) -> str:
        return self._name

    @property
    def parked_for(self) -> float:
        """Seconds left on this lane's cooldown, 0.0 when it is free.

        A reading, not an instruction. Whether to wait it out, send anyway, or
        give up is the caller's call, because how long to wait for a source is a
        property of that source rather than of the transport.
        """
        return max(0.0, self._free_at - self._clock())

    def park(self, seconds: float) -> None:
        """Ask for this lane to be skipped in rotation for `seconds`.

        Called by whoever saw the 429, with a number that whoever it was chose.
        Nothing here sleeps: this only changes which lane `acquire()` prefers.
        """
        self._free_at = self._clock() + seconds

    def get(
        self, url: str, *, headers: Mapping[str, str] | None = None
    ) -> httpx.Response:
        """One GET on this lane's session, whatever the status comes back as.

        Never raises on status. Yahoo answers an expired crumb with a 401, and
        that is information rather than a failure — losing the status behind an
        exception is exactly what stops `fetch()` being usable for it.
        """
        # A run that spread across the zone and a run that quietly collapsed onto
        # one exit look identical from the outside, so which lane served what is
        # worth having. `redact` is why the crumb in the query string is not.
        logger.debug("lane %s: %s", self._name, redact(url))
        return self._client.get(url, headers=dict(headers) if headers else None)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "Lane":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class LanePool:
    """N lanes, handed out in turn, skipping any on cooldown."""

    def __init__(self, lanes: Sequence[Lane]) -> None:
        if not lanes:
            # Same shape as `fetch()` refusing an empty strategy list: a pool
            # with nowhere to send a request is a configuration mistake, not an
            # empty collection to iterate zero times.
            raise ValueError("a lane pool needs at least one lane")
        self._lanes = tuple(lanes)
        self._next = 0

    @classmethod
    def direct(
        cls,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> "LanePool":
        """One lane, no proxy — the box's own address.

        Reads no configuration at all, so a pool built this way cannot reach
        Bright Data however the environment is set. That is the promise
        `DEFAULT_STRATEGIES` makes for `fetch()`, kept the same way and enforced
        by the same kind of test.
        """
        return cls(
            [
                Lane(
                    DIRECT_LANE,
                    headers=headers,
                    timeout=timeout,
                    transport=transport,
                )
            ]
        )

    @classmethod
    def from_env(
        cls,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
        fallback_to_direct: bool = False,
    ) -> "LanePool":
        """One lane per configured Bright Data exit.

        Raises `StrategyUnavailable` when nothing is configured rather than
        quietly handing back a direct pool: a caller that asked for proxied lanes
        and silently got its own IP is the failure this module exists to make
        visible. `fallback_to_direct` is for the one caller that starts on the
        proxy by default and must still behave exactly as it always has on a
        machine where Bright Data was never set up — clearing the addresses is
        then also how that caller is switched off.
        """
        pairs = ProxyConfig.from_env().lane_urls()
        if not pairs:
            if fallback_to_direct:
                return cls.direct(
                    headers=headers, timeout=timeout, transport=transport
                )
            raise StrategyUnavailable(
                "lanes: BRIGHTDATA_PROXY_IPS is unset, or no proxy credentials"
            )
        return cls(
            [
                Lane(
                    name,
                    proxy=url,
                    headers=headers,
                    timeout=timeout,
                    transport=transport,
                )
                for name, url in pairs
            ]
        )

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(lane.name for lane in self._lanes)

    def acquire(self) -> Lane:
        """The next lane in turn, skipping any still on cooldown.

        Never raises and never sleeps. Rotation happens on every call rather than
        only after a failure: a pool that stayed on one lane until it broke would
        put a whole run's requests through one address and leave the rest idle,
        which is the rotation not happening. When every lane is parked this hands
        back the one that frees soonest, with `parked_for` still set, because
        deciding the caller should wait is the caller's — see D6.
        """
        count = len(self._lanes)
        for offset in range(count):
            lane = self._lanes[(self._next + offset) % count]
            if not lane.parked_for:
                self._next = (self._next + offset + 1) % count
                return lane

        soonest = min(self._lanes, key=lambda lane: lane.parked_for)
        logger.warning(
            "every lane is on cooldown; %s frees soonest, in %.1fs",
            soonest.name,
            soonest.parked_for,
        )
        return soonest

    def close(self) -> None:
        for lane in self._lanes:
            lane.close()

    def __len__(self) -> int:
        return len(self._lanes)

    def __enter__(self) -> "LanePool":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

