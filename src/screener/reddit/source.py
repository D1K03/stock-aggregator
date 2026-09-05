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
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from screener.fetch import fetch

logger = logging.getLogger(__name__)

# Their cap. A larger `limit` is answered with a 400, not a truncated page.
PAGE = 100

MAX_ATTEMPTS = 6

# What Arctic Shift answers when it wants less traffic. 429 is the usual one;
# 422 is theirs, and its body says so in words: {"error": "Timeout. Maybe slow
# down a bit"}. Treating that as a hard failure ends a backfill early with most
# of the window unread and a count that still looks plausible.
#
# Measured on the VPS this runs on, walking a day of r/wallstreetbets comments:
# 20 refusals across 127 pages, about one in six, all of which the retry below
# recovered. An earlier run of sixty pages saw none, which is only worth
# recording because it is the wrong conclusion: the refusals arrive as the walk
# goes deeper, so a shallow test says the opposite of the truth.
THROTTLED = ("422", "429")

# How much of the timeline to ask for at once. Politeness rather than necessity:
# a bounded window is a cheaper query for a volunteer-run service to answer than
# a week paginated deeply, and it costs nothing to ask that way. It is *not* the
# fix for the refusal above, which the box never produced.
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

    A page shorter than `PAGE` ends the walk. `sleep` is injected so a test
    asserts the delays without waiting, as `universe.sources.yahoo` does.
    """
    if kind not in ENDPOINT:
        raise ValueError(f"unknown kind {kind!r}")
    pause = sleep or time.sleep
    seen: set[str] = set()

    # Newest window first, so an interrupted backfill has the recent end rather
    # than a week-old fragment.
    window_end = before
    while window_end > after:
        window_start = max(after, window_end - WINDOW)
        yield from _window(
            kind, subreddit,
            after=window_start, before=window_end, host=host,
            delay=delay, pause=pause, transport=transport, seen=seen,
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
) -> Iterator[Item]:
    """One bounded window, paginated backwards a page at a time."""
    cursor = before

    while cursor > after:
        url = (
            f"{host}/{ENDPOINT[kind]}"
            f"?subreddit={subreddit}"
            f"&after={int(after.timestamp())}"
            f"&before={int(cursor.timestamp())}"
            f"&limit={PAGE}"
        )
        page = _page(url, delay=delay, pause=pause, transport=transport)
        if not page:
            return

        oldest = cursor
        for raw in page:
            item = _item(kind, raw)
            if item is None or item.external_id in seen:
                continue
            seen.add(item.external_id)
            oldest = min(oldest, item.created_utc)
            yield item

        if len(page) < PAGE:
            return
        if oldest >= cursor:
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
        pause(delay)


def _page(
    url: str,
    *,
    delay: float,
    pause: Callable[[float], None],
    transport: httpx.BaseTransport | None,
) -> list[dict]:
    """One page, with backoff on a rate limit. Raises `SourceError` on nonsense."""
    backoff = max(delay, 1.0)
    for _ in range(MAX_ATTEMPTS):
        try:
            result = fetch(url, headers=HEADERS, timeout=TIMEOUT, transport=transport)
        except Exception as exc:
            # `fetch` raises on any non-2xx, so a throttle arrives here as text
            # rather than as a status.
            if not any(code in str(exc) for code in THROTTLED):
                raise SourceError(f"arctic shift refused: {type(exc).__name__}") from exc
            pause(backoff)
            backoff *= 2
            continue
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
    raise SourceError("arctic shift asked us to slow down on every attempt")
