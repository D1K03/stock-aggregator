"""One URL in, one document out.

The only module here that opens both a socket and a database connection, and
the only place the ladder, the blob store, the two tables and the audit trail
meet. Everything it calls is testable without it: `urls` is pure, `extract`
never opens a socket, `store` never opens one either, and `acquire` needs no
database.

The order matters and is worth stating, because each step exists to stop the
next one from costing something:

    refused before?  ->  robots  ->  meter  ->  ladder  ->  extract  ->  store

A link that was refused yesterday is not re-fetched today. robots.txt is read
before the page. The billed rung is dropped before the fetch rather than
regretted after it. And the page is only stored once it has proved to be an
article.
"""

import logging
import threading
import time
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal

import psycopg

from screener.audit import record
from screener.blobs import BlobStore
from screener.blobs import store as blob_store
from screener.config import settings
from screener.magpie import store
from screener.magpie.acquire import NotAPage, NotPermitted, acquire
from screener.magpie.config import MagpieConfig
from screener.magpie.extract import NotAnArticle, extract
from screener.magpie.models import Document, Refused
from screener.magpie.urls import canonical, host_of
from screener.fetch.result import FetchError
from screener.fetch.strategies import redact

logger = logging.getLogger(__name__)

OPERATION = "magpie.scrape"
CONNECT_TIMEOUT = 5


class _Politeness:
    """A floor between two requests to one host.

    One person pasting one link never meets this. It exists so the crawler does
    not have to retrofit politeness into a call path that never had any — and
    when the crawler lands this moves into the claim query, because a sleeping
    worker holds a slot and a queue that declines to hand out a row does not.
    """

    def __init__(self, sleep=time.sleep) -> None:
        self._last: dict[str, float] = {}
        self._sleep = sleep
        self._lock = threading.Lock()

    def wait(self, host: str, delay: float) -> None:
        with self._lock:
            seen = self._last.get(host)
            now = time.monotonic()
            owed = 0.0 if seen is None else max(0.0, delay - (now - seen))
            self._last[host] = now + owed
        if owed:
            self._sleep(owed)


_POLITE = _Politeness()


@dataclass(frozen=True, slots=True)
class Outcome:
    """What one scrape produced. Exactly one of these two is set."""

    document: Document | None = None
    refused: Refused | None = None


def scrape(
    url: str,
    *,
    config: MagpieConfig | None = None,
    conn: psycopg.Connection | None = None,
    blobs: BlobStore | None = None,
    requested_by: str | None = None,
    may_pay: bool = True,
    transport=None,
) -> Outcome:
    """Fetch, extract and store one page. Never raises.

    A refusal is a result rather than an exception: every caller — the tool, the
    API, and eventually the crawler — wants to say why rather than to handle an
    error, and a scraper whose ordinary outcomes are exceptions gets wrapped in
    a bare `except` by the second caller.
    """
    config = config or MagpieConfig.from_env()

    if conn is not None:
        return _scrape(url, config, conn, blobs or blob_store(), requested_by, may_pay, transport)

    try:
        with psycopg.connect(
            settings().database_url, connect_timeout=CONNECT_TIMEOUT, autocommit=True
        ) as owned:
            return _scrape(url, config, owned, blobs or blob_store(), requested_by, may_pay, transport)
    except psycopg.Error as exc:
        logger.warning("magpie could not reach the database: %s", exc)
        return Outcome(refused=Refused(url=url, reason="unavailable", detail=str(exc)[:200]))


def _scrape(
    url: str,
    config: MagpieConfig,
    conn: psycopg.Connection,
    blobs: BlobStore,
    requested_by: str | None,
    may_pay: bool,
    transport=None,
) -> Outcome:
    key = canonical(url)
    host = host_of(key)

    # Asked before, and told no. Re-fetching would read the same robots.txt to
    # reach the same answer, and on a paywall it would do it down a billed rung.
    previous = store.last_refusal(conn, key)
    if previous and previous[0] in ("robots", "paywall"):
        return Outcome(refused=Refused(url=key, reason=previous[0], detail=previous[1]))

    # The meter, before the fetch. `None` means unreadable, and unreadable drops
    # the billed rung rather than refusing: a capped scrape still tries the free
    # strategies, so this fails closed without failing shut.
    used = store.unlocker_used_today(conn)
    capped = used is None or used >= config.unlocker_daily_max
    allowed_to_pay = may_pay and not capped

    attempt = store.begin_attempt(conn, url=url, requested_by=requested_by)
    started = time.perf_counter()

    def settle(**kwargs) -> None:
        store.finish_attempt(conn, attempt, **kwargs)

    try:
        _POLITE.wait(host, config.host_delay)
        fetched = acquire(url, config, may_pay=allowed_to_pay, transport=transport)
    except NotPermitted as exc:
        settle(state="refused", reason="robots", error=str(exc))
        return Outcome(refused=Refused(url=key, reason="robots", detail=str(exc)))
    except NotAPage as exc:
        reason = "paywall" if "free to read" in str(exc) else "not_a_page"
        settle(state="refused", reason=reason, error=str(exc))
        return Outcome(refused=Refused(url=key, reason=reason, detail=str(exc)))
    except FetchError as exc:
        reason = "unlocker_capped" if capped and may_pay else "all_strategies_failed"
        settle(state="failed", reason=reason, error=redact(str(exc)))
        detail = (
            "every route failed, and the paid one was over its daily limit"
            if reason == "unlocker_capped"
            else "every route failed"
        )
        return Outcome(refused=Refused(url=key, reason=reason, detail=detail))

    cost = fetched.cost_usd

    try:
        article = extract(fetched.html, url=key)
    except NotAnArticle as exc:
        settle(
            state="refused", reason="too_short", strategy=fetched.strategy,
            attempts=fetched.attempts, status_code=fetched.status_code,
            payload_bytes=len(fetched.html), cost_usd=cost, error=str(exc),
        )
        _bill(cost, fetched, requested_by, key, outcome="error", started=started)
        return Outcome(refused=Refused(url=key, reason="too_short", detail=str(exc)))

    digest = store.content_hash(article)
    path = store.put_payload(blobs, host, date.today(), digest, fetched.html)
    document = store.save(
        conn,
        url=key,
        article=article,
        strategy=fetched.strategy,
        status_code=fetched.status_code,
        blob_path=path,
        digest=digest,
        requested_by=requested_by,
    )

    # The climb belongs on the document, not only in the attempt row: an
    # interface showing what a page cost should not have to join to get it.
    document = replace(document, attempts=fetched.attempts, cost_usd=float(cost))

    settle(
        state="stored" if document.stored else "unchanged",
        strategy=fetched.strategy,
        attempts=fetched.attempts,
        status_code=fetched.status_code,
        payload_bytes=len(fetched.html),
        cost_usd=cost,
        document_id=document.id,
    )
    _bill(cost, fetched, requested_by, key, outcome="ok", started=started)
    return Outcome(document=document)


def _bill(
    cost: Decimal,
    fetched,
    requested_by: str | None,
    url: str,
    *,
    outcome: str,
    started: float,
) -> None:
    """Put the fetch on the audit trail, with what it cost.

    Recorded as `system` rather than `tool` on purpose. The cost is real and
    belongs on `/audit` beside everything else that spends money — but
    `budget.spent_24h()` sums `cost_usd` over every event to decide whether
    somebody may ask Steven another question, and proxy spend is forty times a
    reply. Letting it land on that meter would mean fifty scrapes silenced the
    assistant for the day. `magpie.attempt` is the meter; this is the ledger.
    """
    record(
        kind="system",
        operation=OPERATION,
        actor=requested_by or "system",
        actor_kind="system",
        outcome=outcome,
        cost_usd=cost,
        duration_ms=int((time.perf_counter() - started) * 1000),
        detail={
            "url": redact(url),
            "strategy": fetched.strategy,
            "attempts": list(fetched.attempts),
            "bytes": len(fetched.html),
        },
    )
