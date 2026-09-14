"""One pass over every configured subreddit: backfill, then keep up."""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import psycopg

from screener.audit import record
from screener.config import settings
from screener.reddit import source as arctic
from screener.reddit.config import RedditConfig
from screener.reddit.store import (
    SOURCE_CODE,
    bump_gap,
    close_gap,
    earliest_seen,
    finish_run,
    latest_seen,
    pending_gaps,
    record_gap,
    save,
    source_id,
    start_run,
)

logger = logging.getLogger(__name__)

KINDS = ("post", "comment")

# How far back to re-walk on an incremental pass. A page boundary lands mid
# second, and an item written during the fetch itself would otherwise fall in
# the gap between where this run stopped and where the next one starts. The
# upsert makes the overlap free.
OVERLAP = timedelta(minutes=5)

# How close to the backfill target counts as having reached it. Without a
# tolerance the gap span is re-walked forever, because the oldest item in a
# subreddit is never exactly on the boundary asked for.
REACHED = timedelta(hours=1)

# Rows held before writing. Large enough that a week of comments is not a
# million round trips, small enough that a failure halfway through has still
# banked most of the work.
BATCH = 500


@dataclass(frozen=True, slots=True)
class Report:
    subreddit: str
    kind: str
    seen: int
    stored: int
    edited: int
    backfilled: bool
    failure: str | None = None


def once(
    config: RedditConfig | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    sleep=None,
    now: datetime | None = None,
) -> list[Report]:
    """Fetch and store every configured subreddit, once. Never raises."""
    config = config or RedditConfig.from_env()
    if not config.enabled:
        logger.info("no subreddits configured; nothing to ingest")
        return []

    moment = now or datetime.now(UTC)
    reports: list[Report] = []
    with psycopg.connect(settings().database_url, autocommit=True) as conn:
        source = source_id(conn, SOURCE_CODE)
        for subreddit in config.subreddits:
            for kind in KINDS:
                reports.append(
                    _walk(
                        conn, source, subreddit, kind, config,
                        moment=moment, transport=transport, sleep=sleep,
                    )
                )

    total_seen = sum(r.seen for r in reports)
    total_stored = sum(r.stored for r in reports)
    total_edited = sum(r.edited for r in reports)
    failed = [f"{r.subreddit}/{r.kind}" for r in reports if r.failure]
    # One row for the whole pass, not one per item: `record` opens a fresh
    # connection per call and the same table backs Steven's memory and the
    # audit page, so per-item rows would flood both.
    #
    # `outcome` follows the spans rather than the call. This wrote 'ok' for a
    # pass that had lost three hours of r/stocks, because nothing here raised —
    # the trail agreeing with the logs is the difference between a hole being
    # noticed and being invisible.
    record(
        kind="system",
        operation="reddit.ingest",
        outcome="partial" if failed else "ok",
        detail={
            "subreddits": list(config.subreddits),
            "seen": total_seen,
            "stored": total_stored,
            "edited": total_edited,
            "backfilled": [f"{r.subreddit}/{r.kind}" for r in reports if r.backfilled],
            "failed": failed,
        },
    )
    logger.info(
        "reddit: %d seen, %d new, %d edited%s",
        total_seen, total_stored, total_edited,
        f", {len(failed)} stream(s) interrupted: {', '.join(failed)}" if failed else "",
    )
    return reports


def queue(
    days: int,
    config: RedditConfig | None = None,
    *,
    streams: Sequence[str] | None = None,
    now: datetime | None = None,
) -> list[tuple[str, str]]:
    """Queue the last `days` for a re-walk, and say what was queued.

    The repair path for holes that predate `social_gap`, and deliberately not a
    hole detector. A quiet hour and a lost hour look identical from here — the
    only place that knows which is which is the mirror — so this queues the
    whole stretch and lets the ordinary drain sort it out. Re-walking what is
    already stored is close to free: `save` writes nothing for an item whose
    text has not changed, which is almost all of them.

    `streams` narrows it to named `subreddit/kind` pairs, and is worth having
    rather than always doing all four: the refusal that causes holes tracks how
    thin a subreddit is, so the damage is concentrated in one stream while a
    week of r/wallstreetbets comments is 130,000 items and over an hour of
    walking that nothing needed.
    """
    config = config or RedditConfig.from_env()
    moment = now or datetime.now(UTC)
    wanted = _streams(config, streams)
    queued: list[tuple[str, str]] = []
    with psycopg.connect(settings().database_url, autocommit=True) as conn:
        source = source_id(conn, SOURCE_CODE)
        for subreddit, kind in wanted:
            record_gap(
                conn, source, subreddit, kind,
                after=moment - timedelta(days=days), before=moment,
                reason="requested",
            )
            queued.append((subreddit, kind))
    logger.info(
        "queued %d day(s) for re-walk: %s",
        days, ", ".join(f"{s}/{k}" for s, k in queued),
    )
    return queued


def _streams(
    config: RedditConfig, streams: Sequence[str] | None
) -> list[tuple[str, str]]:
    """`subreddit/kind` pairs, defaulting to every one configured.

    A named stream is checked against the configuration rather than taken as
    given: a typo would otherwise queue a gap nothing ever drains, which is a
    row that says data is missing and quietly means nothing is looking for it.
    """
    every = [(s, k) for s in config.subreddits for k in KINDS]
    if not streams:
        return every
    chosen: list[tuple[str, str]] = []
    for name in streams:
        subreddit, _, kind = name.partition("/")
        pair = (subreddit.strip().lstrip("r/").strip(), kind.strip())
        if pair not in every:
            raise ValueError(
                f"{name!r} is not a configured stream; "
                f"try one of {', '.join(f'{s}/{k}' for s, k in every)}"
            )
        chosen.append(pair)
    return chosen


def _walk(
    conn: psycopg.Connection,
    source: int,
    subreddit: str,
    kind: str,
    config: RedditConfig,
    *,
    moment: datetime,
    transport: httpx.BaseTransport | None,
    sleep,
) -> Report:
    """One subreddit, one kind. Records its own ingest_run either way."""
    newest = latest_seen(conn, source, subreddit, kind)
    oldest = earliest_seen(conn, source, subreddit, kind)
    target = moment - timedelta(days=config.backfill_days)

    # Three kinds of span, and only the first two were ever here. The walk runs
    # backwards from now, so an interruption leaves the newest slice stored and
    # everything below the point it died missing.
    #
    # `(target, oldest)` repairs the *old end* — a backfill that never finished.
    # It cannot repair anything else, because it is bounded by `oldest` and by
    # construction never looks inside what has already been walked. So once the
    # backfill was complete it stopped firing, and from then on every
    # interrupted pass burned a hole: the newer items were banked, `latest_seen`
    # jumped to the present, and nothing looked between the two again.
    #
    # `pending_gaps` is the third kind and the one that closes that.
    #
    # Catch-up still goes first, for the reason the old end always went second:
    # a fresh comment should never wait behind a long repair, and a repair is
    # the one span here with no upper bound on how long it takes.
    spans: list[tuple[int | None, datetime, datetime]] = []
    backfilled = newest is None
    if newest is None:
        spans.append((None, target, moment))
    else:
        spans.append((None, newest - OVERLAP, moment))
    spans.extend(pending_gaps(conn, source, subreddit, kind))
    if newest is not None and oldest is not None and oldest > target + REACHED:
        # Whatever the last pass did not reach at the old end.
        spans.append((None, target, oldest))
        backfilled = True
    run_id = start_run(conn, source, f"{subreddit}/{kind}")
    seen = stored = edited = 0
    failure: str | None = None
    for gap_id, span_after, span_before in spans:
        batch: list[arctic.Item] = []
        # How far back this span actually got. The walk yields newest first, so
        # the oldest item banked is the frontier, and everything below it is
        # what the next pass has to come back for.
        reached = span_before
        try:
            for item in arctic.items(
                kind, subreddit,
                after=span_after, before=span_before, host=config.host,
                delay=config.delay, sleep=sleep, transport=transport,
            ):
                seen += 1
                reached = min(reached, item.created_utc)
                batch.append(item)
                if len(batch) >= BATCH:
                    new, changed = save(conn, source, batch)
                    stored += new
                    edited += changed
                    batch = []
            if batch:
                new, changed = save(conn, source, batch)
                stored += new
                edited += changed
            if gap_id is not None:
                close_gap(conn, gap_id)
        except Exception as exc:
            # Bank what this span already has, then write down the rest as a
            # span rather than losing it. Banking first matters for the
            # bookkeeping as well as the data: an item that is not stored is not
            # covered, so a batch that cannot be written widens the gap to the
            # whole span rather than trusting `reached`.
            if batch:
                try:
                    new, changed = save(conn, source, batch)
                    stored += new
                    edited += changed
                except Exception:
                    logger.warning("could not bank the final batch for %s", subreddit)
                    reached = span_before
            if reached < span_before:
                # Progress was made, so the remainder below the frontier is what
                # is outstanding. It *replaces* a gap this span came from:
                # keeping both would re-walk the part that worked on every pass
                # from here on.
                record_gap(
                    conn, source, subreddit, kind,
                    after=span_after, before=reached,
                )
                if gap_id is not None:
                    close_gap(conn, gap_id)
            elif gap_id is not None:
                # A gap that yielded nothing at all. Leave the row — it is still
                # the record of the hole — but count the attempt, or an
                # unanswerable span sits at the front of every pass forever.
                bump_gap(conn, gap_id)
            # A catch-up or backfill span that yielded nothing needs no row:
            # `latest_seen` and `earliest_seen` have not moved either, so the
            # next pass computes the same span again. It is only progress that
            # makes them unable to describe what is left.
            failure = failure or str(exc)
            logger.warning(
                "%s/%s failed after %d items, %s..%s left to re-walk: %s",
                subreddit, kind, seen, span_after, reached, exc,
            )
            # On to the next span rather than out of the loop. Stopping here is
            # what kept the repair from ever running: `stocks/comment` fails its
            # catch-up more often than not, and a `break` means the gaps queued
            # behind it are never drained on any pass that would need them. The
            # spans cover different stretches of the timeline and the mirror
            # refuses them independently.
            continue

    if failure is not None:
        finish_run(conn, run_id, "partial" if stored else "failed", failure)
        return Report(subreddit, kind, seen, stored, edited, backfilled, failure)

    finish_run(conn, run_id, "ok")
    logger.info(
        "%s/%s: %d seen, %d new, %d edited%s",
        subreddit, kind, seen, stored, edited, " (backfill)" if backfilled else "",
    )
    return Report(subreddit, kind, seen, stored, edited, backfilled)
