"""One pass over every configured subreddit: backfill, then keep up."""

import logging
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
    earliest_seen,
    finish_run,
    latest_seen,
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
    # One row for the whole pass, not one per item: `record` opens a fresh
    # connection per call and the same table backs Steven's memory and the
    # audit page, so per-item rows would flood both.
    record(
        kind="system",
        operation="reddit.ingest",
        detail={
            "subreddits": list(config.subreddits),
            "seen": total_seen,
            "stored": total_stored,
            "edited": total_edited,
            "backfilled": [f"{r.subreddit}/{r.kind}" for r in reports if r.backfilled],
        },
    )
    logger.info(
        "reddit: %d seen, %d new, %d edited", total_seen, total_stored, total_edited
    )
    return reports


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

    # Two spans, not one, and this is the difference between a backfill that
    # finishes and one that leaves a permanent hole. The walk runs backwards
    # from now, so an interruption — and on a busy subreddit the mirror refuses
    # roughly one page in six — leaves the newest slice stored and the older end
    # missing. Resuming from the newest item alone never comes back for it.
    spans: list[tuple[datetime, datetime]] = []
    backfilled = newest is None
    if newest is None:
        spans.append((target, moment))
    else:
        spans.append((newest - OVERLAP, moment))
        if oldest is not None and oldest > target + REACHED:
            # Whatever the last pass did not reach. Older span second, so a
            # fresh comment is never delayed behind a long catch-up.
            spans.append((target, oldest))
            backfilled = True
    run_id = start_run(conn, source, f"{subreddit}/{kind}")
    seen = stored = edited = 0
    failure: str | None = None
    for span_after, span_before in spans:
        batch: list[arctic.Item] = []
        try:
            for item in arctic.items(
                kind, subreddit,
                after=span_after, before=span_before, host=config.host,
                delay=config.delay, sleep=sleep, transport=transport,
            ):
                seen += 1
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
        except Exception as exc:
            # Bank what this span already has: the next pass resumes from it,
            # and the gap span above is what eventually closes the rest.
            if batch:
                try:
                    new, changed = save(conn, source, batch)
                    stored += new
                    edited += changed
                except Exception:
                    logger.warning("could not bank the final batch for %s", subreddit)
            failure = str(exc)
            logger.warning("%s/%s failed after %d items: %s", subreddit, kind, seen, exc)
            break

    if failure is not None:
        finish_run(conn, run_id, "partial" if stored else "failed", failure)
        return Report(subreddit, kind, seen, stored, edited, backfilled)

    finish_run(conn, run_id, "ok")
    logger.info(
        "%s/%s: %d seen, %d new, %d edited%s",
        subreddit, kind, seen, stored, edited, " (backfill)" if backfilled else "",
    )
    return Report(subreddit, kind, seen, stored, edited, backfilled)
