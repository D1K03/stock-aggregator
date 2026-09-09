"""`python -m screener.nightly` -- the scheduler container's entry point.

Runs the pipeline once a night at `NIGHTLY_TRIGGER_HOUR` UTC, catching up a
night a restart interrupted and reporting one it had to give up on.

The wait is a `threading.Event`, not `time.sleep`, for the reason
`screener.reddit` gives: a signal handler cannot interrupt a long sleep, so
SIGTERM would be answered whenever the sleep happened to end and every deploy
would sit through the full SIGKILL timeout. This is PID 1 in the container.
"""

import logging
import signal
import threading
from datetime import date, datetime, timezone
from typing import Any

import psycopg

from screener.blobs import store
from screener.config import settings
from screener.ingest import ChartClient, TimeseriesClient
from screener.nightly.config import BACKOFF_SECONDS, NightlyConfig
from screener.nightly.night import NightReport, already_scored, run_night
from screener.nightly.schedule import is_due, next_trigger
from screener.notify import Alert, ChannelError, DiscordWebhook, NotificationChannel
from screener.scoring import NoBarsVisible, ScoringInProgress
from screener.secrets import SecretsError, load_into_environ

logger = logging.getLogger(__name__)

stopping = threading.Event()


def _channel() -> NotificationChannel:
    """The delivery channel, built late so an unset webhook is not fatal at import."""
    return DiscordWebhook()


def announce(day: date, reason: str) -> None:
    """Say that a night was lost. Once, and only when one was.

    This is not an alert. It reads no `alert_rule`, computes no crossing and
    touches no `emits_alerts` flag, so it neither pre-empts the alerting cycle
    nor breaks the scoring cycle's claim that its runs stay silent.
    """
    try:
        channel = _channel()
    except ChannelError:
        # A missing webhook must not turn a failed night into a crash loop.
        logger.warning(
            "no Discord webhook configured; the lost night for %s is only in this log",
            day,
        )
        return
    try:
        channel.send(
            Alert(
                title=f"Nightly run failed for {day}",
                body=(
                    f"{reason}\n\n"
                    "This snapshot cannot be backfilled. Scoring is forward-only "
                    "and `--as-of` refuses a past date, so this date is a "
                    "permanent hole in the forward log rather than something "
                    "tomorrow repairs."
                ),
                severity="warning",
            )
        )
    except ChannelError as exc:
        logger.error("could not report the lost night for %s: %s", day, exc)


def _attempt(config: NightlyConfig, today: date) -> NightReport:
    """One attempt at tonight, with its own connection and its own clients."""
    with psycopg.connect(settings().database_url, autocommit=True) as conn:
        with ChartClient() as chart, TimeseriesClient() as timeseries:
            return run_night(
                conn, today=today, blobs=store(), chart=chart, timeseries=timeseries
            )


def run_tonight(config: NightlyConfig, today: date) -> bool:
    """Try tonight, up to `config.attempts` times. True when it scored."""
    reason = "the night did not run"
    for attempt in range(1, config.attempts + 1):
        try:
            report = _attempt(config, today)
            if report.ok:
                logger.info("night %s complete", today)
                return True
            reason = (
                f"prices wholly failed: 0 of {report.prices.requested} securities "
                "answered, so scoring was skipped rather than score stale bars"
            )
        except ScoringInProgress as exc:
            # Another process holds the lock, which means a run is working.
            # Retrying would be arguing with it.
            logger.info("%s", exc)
            return False
        except NoBarsVisible as exc:
            # Not transient. Three attempts would delay nothing but this line.
            logger.error("%s", exc)
            announce(today, str(exc))
            return False
        except Exception as exc:
            logger.exception("night %s, attempt %d of %d failed", today, attempt, config.attempts)
            # Bounded: `httpx` and `psycopg` exception text can carry request
            # URLs and DSNs, and a Bright Data lane URL carries credentials in
            # the URL itself. This string reaches Discord, so an unlucky
            # exception must not be able to post one.
            reason = f"{type(exc).__name__}: {str(exc)[:500]}"

        if attempt < config.attempts and not stopping.is_set():
            # Five minutes, then fifteen. A night is ~3,000 Yahoo requests, so
            # a tight retry loop against a source having a bad evening is how a
            # rate limit nobody has hit becomes one that has been.
            stopping.wait(BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)])
        if stopping.is_set():
            # Being torn down. Do not sit through the remaining backoffs, and
            # do not report a night that was interrupted rather than lost --
            # the next boot's catch-up check will finish it.
            return False

    announce(today, reason)
    return False


def _tick(config: NightlyConfig, now: datetime) -> None:
    """The catch-up decision for one wake-up: D3, extracted so it is testable
    without a real clock, a real wait or a real database.

    Both halves of the catch-up condition. Without the second a restart would
    score a night twice; without the first a container starting at 10:00
    would run the night thirteen hours early, against a market still open.
    """
    if not is_due(now, config.trigger_hour):
        return
    try:
        with psycopg.connect(settings().database_url, autocommit=True) as conn:
            owed = not already_scored(conn, now.date())
    except Exception:
        # A dead database here must not escape into a restart loop -- that
        # would discard `run_tonight`'s own attempt counter and backoffs at
        # exactly the moment they matter. Fall through to the wait; `is_due`
        # is still true next pass.
        logger.exception("could not check whether %s is already scored", now.date())
        owed = False
    if owed:
        run_tonight(config, now.date())


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    try:
        load_into_environ()
    except SecretsError as exc:
        logger.error("%s", exc)
        return 1

    config = NightlyConfig.from_env()
    if not config.enabled:
        # Not an error. `restart: unless-stopped` restarts on any exit code,
        # not just failure, so this process exits and comes straight back --
        # it will read the same env on the next boot and exit again. That is
        # a restart loop rather than a wait, but a cheap one: no connection,
        # no I/O, nothing but reading one env var each time. Re-enabling still
        # needs the container recreated, because `environment:` is baked in
        # at create time.
        logger.info("NIGHTLY_ENABLED is false; not scheduling")
        return 0

    def stop(*_: Any) -> None:
        logger.info("stopping after the current step")
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    logger.info("scheduling a night at %02d:00 UTC", config.trigger_hour)

    while not stopping.is_set():
        _tick(config, datetime.now(timezone.utc))

        if stopping.is_set():
            break
        wait = (
            next_trigger(datetime.now(timezone.utc), config.trigger_hour)
            - datetime.now(timezone.utc)
        ).total_seconds()
        logger.info("next night in %.1f hours", wait / 3600)
        stopping.wait(wait)

    logger.info("nightly scheduler stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
