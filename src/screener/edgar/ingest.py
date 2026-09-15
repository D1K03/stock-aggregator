"""One pass over every published day EDGAR has that we have not walked."""

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import httpx
import psycopg

from screener.audit import record
from screener.config import settings
from screener.edgar import source as edgar
from screener.edgar.config import EdgarConfig
from screener.edgar.store import (
    ENDPOINT_PREFIX,
    SOURCE_CODE,
    finish_run,
    save,
    source_id,
    start_run,
    universe_ciks,
    walked,
)

logger = logging.getLogger(__name__)

# Rows held before writing. Large enough that a day is not 174 round trips,
# small enough that a failure halfway through has still banked most of the work.
BATCH = 500


@dataclass(frozen=True, slots=True)
class Report:
    """What one day's walk did."""

    day: date
    filings: int
    seen: int
    stored: int
    edited: int
    failure: str | None = None


def once(
    config: EdgarConfig | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    sleep=None,
    now: datetime | None = None,
) -> list[Report]:
    """Walk the unwalked days, newest first, up to `days_per_pass`. Never raises."""
    config = config or EdgarConfig.from_env()
    if not config.enabled:
        logger.info("no EDGAR contact address configured; nothing to ingest")
        return []

    moment = now or datetime.now(UTC)
    today = moment.date()
    earliest = today - timedelta(days=config.backfill_days)

    reports: list[Report] = []
    refused = False
    with psycopg.connect(settings().database_url, autocommit=True) as conn:
        source = source_id(conn, SOURCE_CODE)
        securities = universe_ciks(conn)
        if not securities:
            # A real state on a fresh database, and one that would otherwise
            # look exactly like a healthy pass that found nothing.
            logger.warning("no securities with a CIK; run `universe load` first")
            return []

        try:
            published = _published(
                earliest, today, config=config, transport=transport
            )
        except Exception as exc:
            logger.error("could not read EDGAR's published days: %s", exc)
            record(
                kind="system",
                operation="edgar.ingest",
                outcome="refused" if isinstance(exc, edgar.Throttled) else "error",
                detail={"days": [], "error": str(exc)[:500]},
            )
            return []

        done = walked(conn, source)
        # Newest first, so a fresh deployment has yesterday before it has last
        # month, and an interrupted backfill leaves the recent end complete --
        # the ordering `screener.reddit` walks a subreddit in, for the reason it
        # gives there.
        todo = sorted((d for d in published if d not in done), reverse=True)
        todo = todo[: max(1, config.days_per_pass)]
        if not todo:
            logger.info("edgar: every published day in the window is already walked")

        for day in todo:
            report = _walk(
                conn, source, securities, day, config,
                transport=transport, sleep=sleep,
            )
            reports.append(report)
            if report.failure is not None and _was_refusal(report.failure):
                # SEC is refusing this address, so the next day would be refused
                # too and every attempt extends the block. End the pass.
                # Deliberately the opposite of `screener.reddit`, which carries
                # on to the next span because Arctic Shift refuses spans
                # independently.
                refused = True
                logger.warning("sec refused us; ending the pass early")
                break

    stored = sum(r.stored for r in reports)
    failed = [r.day.isoformat() for r in reports if r.failure]
    # One row for the whole pass, not one per day: `record` opens a fresh
    # connection per call and the same table backs Steven's memory.
    #
    # The outcome follows the *days* rather than the call, which is the point
    # 026 had to widen a constraint to make: a pass that lost a day while
    # nothing raised would otherwise write 'ok' and the hole would be invisible.
    record(
        kind="system",
        operation="edgar.ingest",
        outcome=("refused" if refused and not stored else "partial" if failed else "ok"),
        detail={
            "days": [r.day.isoformat() for r in reports],
            "filings": sum(r.filings for r in reports),
            "seen": sum(r.seen for r in reports),
            "stored": stored,
            "edited": sum(r.edited for r in reports),
            "failed": failed,
        },
    )
    logger.info(
        "edgar: %d day(s), %d filing(s), %d new, %d edited%s",
        len(reports), sum(r.filings for r in reports), stored,
        sum(r.edited for r in reports),
        f", {len(failed)} day(s) incomplete: {', '.join(failed)}" if failed else "",
    )
    return reports


def _published(
    earliest: date,
    today: date,
    *,
    config: EdgarConfig,
    transport: httpx.BaseTransport | None,
) -> list[date]:
    """Every day in the window EDGAR actually published an index for.

    One request per quarter, and it is what makes a missing day cost nothing: a
    weekend, a federal holiday and today-before-the-feed-closes are simply
    absent from the listing, so they are never requested. Without this, each
    would be a 403 indistinguishable from SEC refusing us.
    """
    out: list[date] = []
    for year, quarter in edgar.quarters(earliest, today):
        for day in edgar.days(
            year, quarter,
            host=config.host, user_agent=config.user_agent, transport=transport,
        ):
            if earliest <= day <= today:
                out.append(day)
    return out


def _was_refusal(failure: str) -> bool:
    return failure.startswith("sec refused")


def _walk(
    conn: psycopg.Connection,
    source: int,
    securities: dict[str, int],
    day: date,
    config: EdgarConfig,
    *,
    transport: httpx.BaseTransport | None,
    sleep,
) -> Report:
    """One published day. Records its own ingest_run either way.

    **A day with no filings from our universe is still `ok`**, and that is the
    whole reason `ingest_run` can be the frontier: the row is what says the day
    was walked, so "nothing to find" and "never looked" stay distinguishable
    where a count of stored rows could not tell them apart.
    """
    run_id = start_run(conn, source, f"{ENDPOINT_PREFIX}/{day.isoformat()}")
    batch: list[edgar.Transaction] = []
    accessions: set[str] = set()
    seen = stored = edited = 0
    failure: str | None = None
    try:
        for tx in edgar.transactions(
            day,
            ciks=securities.keys(),
            host=config.host,
            user_agent=config.user_agent,
            delay=config.delay,
            sleep=sleep,
            transport=transport,
        ):
            seen += 1
            accessions.add(tx.accession_number)
            batch.append(tx)
            if len(batch) >= BATCH:
                new, changed = save(conn, source, securities, batch)
                stored += new
                edited += changed
                batch = []
        if batch:
            new, changed = save(conn, source, securities, batch)
            stored += new
            edited += changed
            batch = []
    except Exception as exc:
        # Bank what this day already has before recording the failure, so a day
        # that died three quarters of the way through keeps those rows and is
        # retried rather than restarted from nothing. The upsert makes the
        # overlap free.
        if batch:
            try:
                new, changed = save(conn, source, securities, batch)
                stored += new
                edited += changed
            except Exception:
                logger.warning("could not bank the final batch for %s", day)
        failure = str(exc)
        logger.warning("%s failed after %d transaction(s): %s", day, seen, exc)

    if failure is not None:
        # `partial` when something landed, `failed` when nothing did -- the
        # distinction `screener.reddit` draws, and what `walked` counts as an
        # attempt either way.
        finish_run(conn, run_id, "partial" if stored else "failed", failure)
        return Report(day, len(accessions), seen, stored, edited, failure)

    finish_run(conn, run_id, "ok")
    logger.info(
        "%s: %d filing(s), %d transaction(s), %d new, %d edited",
        day, len(accessions), seen, stored, edited,
    )
    return Report(day, len(accessions), seen, stored, edited)
