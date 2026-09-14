"""Posts and comments from Arctic Shift. Returns rows, never writes.

Reddit's own API is not reachable for this. Verified rather than assumed:
`https://www.reddit.com/r/stocks/new.json` answers 403 with an HTML body
whatever User-Agent is sent, and `robots.txt` is `User-agent: * / Disallow: /`.
`CLAUDE.md` says scrapers respect robots.txt and ToS, so scraping it anyway is
ruled out here. The official OAuth route needs a manually approved client and
caps listings at about a thousand items, which does not reach a week of
r/wallstreetbets in any case.

Arctic Shift is a public mirror with date-range search over both posts and
comments, which is what makes a backfill possible at all.

Rate limiting lives in this module rather than in `screener.fetch`, because D6
puts a source's limit with the source. Arctic Shift publishes none and is run by
volunteers, so the delay between pages is politeness and the backoff is for the
429 that a sibling mirror answered a first probe with.
"""

import logging
import time
from collections.abc import Callable, Generator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from screener.fetch import fetch

logger = logging.getLogger(__name__)

# Their cap. A larger `limit` is answered with a 400, not a truncated page.
PAGE = 100

# httpx's exact phrasing for the two refusals, which reach this module as text:
# `screener.fetch` collapses every strategy's exception into one `FetchError`
# message, so there is no status code left to read. Anchored on the quoted
# reason rather than matched as a bare "422", which a timestamp in a URL or a
# byte count in an error would satisfy by accident.
TIMED_OUT = "'422 Unprocessable Entity'"
RATE_LIMITED = "'429 Too Many Requests'"

# **These two are not the same thing, and the difference is the whole of the
# retry policy here.**
#
# 429 is Arctic Shift asking for less traffic. Waiting is the remedy, so the
# same request goes out again after an exponential pause.
#
# 422 carries {"data": null, "error": "Timeout. Maybe slow down a bit"} and the
# wording is misleading: it is *their query* giving up, not us being throttled.
# Measured 2026-09-14 -- it reproduces instantly from an address with no request
# history, every 422 takes ~2.8s against ~1.2s for a page that works, and
# `limit=10` fails exactly as `limit=100` does. None of that is a per-client
# limit; it is a server-side time budget, and it bites hardest on a thin
# subreddit, where filling a hundred-item page means scanning far more of the
# index. On the box `stocks/comment` hit it on 31 runs of 50 and
# `wallstreetbets/comment` on 3 of 51.
#
# So waiting does not help and narrowing does. Five cold three-hour windows
# needed 8, 10 and 8 identical retries to come good and two never came good in
# 12; the window that failed 12 times out of 12 was answered on 11 of 16 first
# tries once it was cut into 675-second slices. `reach` below is that cut.
ATTEMPTS = 3
RETRY_DELAY = 1.0
RATE_LIMIT_ATTEMPTS = 6

# How much of the timeline one *request* may cover. Starts at `WINDOW`, halves
# on a timeout and widens again once the mirror has been answering, so a walk
# settles near the widest slice this subreddit can actually be read in and does
# not pay to rediscover it on every page.
MIN_REACH = timedelta(minutes=5)
# Widening costs `ATTEMPTS` refusals to discover it went too far, so it is
# deliberately slower than narrowing: without it one unlucky refusal early in a
# week-long backfill would read the remaining seven days five minutes at a time,
# and with it any sooner the walk spends a third of its requests overshooting.
WIDEN_AFTER = 8

# How much of the timeline to ask for at once. Politeness rather than necessity:
# a bounded window is a cheaper query for a volunteer-run service to answer than
# a week paginated deeply, and it costs nothing to ask that way.
WINDOW = timedelta(days=1)

TIMEOUT = 30.0

HEADERS = {
    "User-Agent": (
        "screener/0.1 (sentiment ingest; +https://github.com/D1K03/stock-aggregator)"
    )
}

ENDPOINT = {"post": "posts/search", "comment": "comments/search"}


class SourceError(RuntimeError):
    """Arctic Shift answered, but not with anything usable."""


class Throttled(SourceError):
    """The mirror's own query timed out. Ask for less of the timeline.

    A `SourceError` so that a caller catching the general case still catches
    this one; separate so that `_window` can tell "narrow it" apart from
    "nothing here is going to work".
    """


@dataclass(frozen=True, slots=True)
class Item:
    """One post or comment, with Reddit's envelope already dropped.

    What is kept is what a sentiment score would read and what an audit of that
    score would need: who said it, when, how the room voted, and the words. What
    is dropped is flair, awards, and the sixty-odd null fields Reddit sends —
    none of which is evidence for anything.
    """

    kind: str
    external_id: str
    subreddit: str
    author: str | None
    created_utc: datetime
    score: int | None
    title: str | None
    body: str
    permalink: str | None
    parent_id: str | None


def _clean(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    # Reddit reports a removed author or body as a literal marker rather than
    # omitting the field. Storing "[deleted]" as though it were a username would
    # put it in a leaderboard.
    return None if text in ("", "[deleted]", "[removed]") else text


def _item(kind: str, raw: dict) -> Item | None:
    external_id = _clean(raw.get("id"))
    created = raw.get("created_utc")
    if not external_id or created is None:
        return None
    body = (raw.get("selftext") if kind == "post" else raw.get("body")) or ""
    title = _clean(raw.get("title")) if kind == "post" else None
    # A post with a title and no self-text is still a post worth keeping: the
    # title is the text. A comment with no body is not.
    text = str(body).strip()
    if not text and not title:
        return None
    prefix = "t3_" if kind == "post" else "t1_"
    return Item(
        kind=kind,
        external_id=external_id if external_id.startswith(prefix) else prefix + external_id,
        subreddit=str(raw.get("subreddit") or "").strip(),
        author=_clean(raw.get("author")),
        created_utc=datetime.fromtimestamp(float(created), UTC),
        score=int(raw["score"]) if isinstance(raw.get("score"), (int, float)) else None,
        title=title,
        body=text,
        permalink=_clean(raw.get("permalink")),
        parent_id=_clean(raw.get("link_id")) if kind == "comment" else None,
    )


def items(
    kind: str,
    subreddit: str,
    *,
    after: datetime,
    before: datetime,
    host: str,
    delay: float = 1.0,
    sleep: Callable[[float], None] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> Iterator[Item]:
    """Every post or comment in `[after, before)`, newest first.

    **Walks backwards, because Arctic Shift answers newest-first.** That was
    measured, not assumed, and getting it wrong is quiet rather than loud: a
    forward walk takes the newest hundred, jumps its cursor to the end of the
    window and stops, so a week's backfill silently returns one page and every
    count looks plausible. The window's upper edge is what moves, one page at a
    time, until it meets `after`.

    `sleep` is injected so a test asserts the delays without waiting, as
    `universe.sources.yahoo` does.
    """
    if kind not in ENDPOINT:
        raise ValueError(f"unknown kind {kind!r}")
    pause = sleep or time.sleep
    seen: set[str] = set()

    # Carried across windows rather than relearned inside each one. Narrowing
    # costs a run of refusals to discover, and the width that works is a
    # property of how thin the subreddit is, which does not change between one
    # day of it and the next.
    reach = WINDOW

    # Newest window first, so an interrupted backfill has the recent end rather
    # than a week-old fragment.
    window_end = before
    while window_end > after:
        window_start = max(after, window_end - WINDOW)
        reach = yield from _window(
            kind, subreddit,
            after=window_start, before=window_end, host=host,
            delay=delay, pause=pause, transport=transport, seen=seen,
            reach=reach,
        )
        window_end = window_start


def _window(
    kind: str,
    subreddit: str,
    *,
    after: datetime,
    before: datetime,
    host: str,
    delay: float,
    pause: Callable[[float], None],
    transport: httpx.BaseTransport | None,
    seen: set[str],
    reach: timedelta,
) -> Generator[Item, None, timedelta]:
    """One bounded window, paginated backwards a slice at a time.

    Returns the `reach` it finished on, so the next window starts where this one
    left off rather than re-provoking the same run of refusals.

    A request covers `[cursor - reach, cursor)` rather than the whole window, so
    a short page means *this slice* is exhausted and not that the window is.
    Conflating the two is what a fixed-width walk gets wrong once it narrows:
    the first thin slice would end the window with most of it unread.
    """
    cursor = before
    reach = min(reach, before - after)
    clean = 0

    while cursor > after:
        floor = max(after, cursor - reach)
        url = (
            f"{host}/{ENDPOINT[kind]}"
            f"?subreddit={subreddit}"
            f"&after={int(floor.timestamp())}"
            f"&before={int(cursor.timestamp())}"
            f"&limit={PAGE}"
        )
        try:
            page = _page(url, delay=delay, pause=pause, transport=transport)
        except Throttled:
            if reach <= MIN_REACH:
                # Already asking for five minutes at a time. Something about
                # this stretch of their index is not answerable today, and the
                # caller recording an un-walked span is a better answer than
                # halving towards zero.
                raise
            reach = max(MIN_REACH, reach / 2)
            clean = 0
            logger.info(
                "%s/%s: mirror timed out; narrowing to %s at a time",
                subreddit, kind, reach,
            )
            continue

        oldest = cursor
        for raw in page:
            item = _item(kind, raw)
            if item is None or item.external_id in seen:
                continue
            seen.add(item.external_id)
            oldest = min(oldest, item.created_utc)
            yield item

        if len(page) < PAGE:
            # Everything this slice holds. The window is only finished if the
            # slice went all the way down to its floor.
            if floor <= after:
                return reach
            cursor = floor
        elif oldest >= cursor:
            # A full page that did not move the edge means more than `PAGE`
            # items share one second — or that the ordering changed under us.
            # Stepping past loses some of that second, but not stepping never
            # terminates, and a run that hangs is the worse failure.
            logger.warning(
                "%s/%s: a full page did not move back past %s; skipping a second",
                subreddit, kind, cursor,
            )
            cursor = datetime.fromtimestamp(int(cursor.timestamp()) - 1, UTC)
        else:
            cursor = oldest

        # Widen again after a run of clean pages, so one bad stretch does not
        # leave the rest of a week being read five minutes at a time.
        clean += 1
        if clean >= WIDEN_AFTER and reach < before - after:
            reach = min(before - after, reach * 2)
            clean = 0
        pause(delay)

    return reach


def _page(
    url: str,
    *,
    delay: float,
    pause: Callable[[float], None],
    transport: httpx.BaseTransport | None,
) -> list[dict]:
    """One page.

    Raises `Throttled` when the mirror's query kept timing out — the caller's
    job then is to ask for less, not to ask again — and `SourceError` on a rate
    limit that outlasted the backoff or on a body that is not a listing.
    """
    backoff = max(delay, 1.0)
    timeouts = 0
    limited = 0
    while True:
        try:
            result = fetch(url, headers=HEADERS, timeout=TIMEOUT, transport=transport)
        except Exception as exc:
            # `fetch` raises on any non-2xx, so both refusals arrive here as
            # text rather than as a status.
            text = str(exc)
            if TIMED_OUT in text:
                timeouts += 1
                if timeouts >= ATTEMPTS:
                    raise Throttled("arctic shift timed out answering") from exc
                # Cheap enough to be worth trying: about one in eight of these
                # comes good on a repeat. The narrowing is what does the work.
                pause(RETRY_DELAY)
                continue
            if RATE_LIMITED in text:
                limited += 1
                if limited >= RATE_LIMIT_ATTEMPTS:
                    raise SourceError(
                        "arctic shift asked us to slow down on every attempt"
                    ) from exc
                pause(backoff)
                backoff *= 2
                continue
            raise SourceError(f"arctic shift refused: {type(exc).__name__}") from exc
        try:
            payload = result.json()
        except ValueError as exc:
            # A 200 carrying an HTML error or holding page. Without this the
            # walk dies on a JSONDecodeError, which points at the parser rather
            # than at the mirror having answered with something else entirely.
            raise SourceError("arctic shift answered with something that is not JSON") from exc
        if not isinstance(payload, dict) or "data" not in payload:
            raise SourceError("arctic shift returned something that is not a listing")
        data = payload["data"]
        if data is None:
            return []
        if not isinstance(data, list):
            raise SourceError("arctic shift returned a listing that is not a list")
        return data
