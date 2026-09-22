"""`python -m screener.edgar` — the insider-transaction container's entry point.

Wakes every `EDGAR_REFRESH_HOURS` and walks whichever published days it has not
walked yet, newest first, up to `EDGAR_DAYS_PER_PASS` of them.

**There is no backfill subcommand**, unlike `screener.reddit`. The frontier here
is the set of days already walked rather than a span somebody has to queue, so
widening `EDGAR_BACKFILL_DAYS` simply offers more days and the running container
drains them a pass at a time. A second command would be a second process walking
the same days, racing the scheduled one for the same rows.

The wait is a `threading.Event`, not `time.sleep`. A signal handler cannot
interrupt a twelve-hour sleep, so SIGTERM would be answered whenever it happened
to finish and every deploy would sit through the full SIGKILL timeout. This is
PID 1 in the container.
"""

import logging
import signal
import sys
import threading
from typing import Any

from screener.config import env
from screener.edgar.config import BANNED, EdgarConfig
from screener.edgar.ingest import once
from screener.secrets import SecretsError, load_into_environ, watch

logger = logging.getLogger(__name__)

USAGE = "usage: python -m screener.edgar"

stopping = threading.Event()


def _refusal(config: EdgarConfig) -> int | None:
    """The exit code when this configuration cannot run, or None when it can.

    Asked at boot and again before every pass. The contact address lives in
    Infisical and can change under a running process, and one that has been
    unset or mistyped has to stop the walk rather than be sent to SEC.
    """
    if config.enabled:
        return None
    if config.contact_email:
        # A typo, not an off switch, and worth separating: SEC answers 403
        # to a User-Agent carrying github.com as firmly as to one naming
        # nobody, and that 403 reads as a network fault. This has already
        # cost one debugging session in `screener.universe`.
        logger.error(
            "EDGAR_CONTACT_EMAIL must not contain %r: SEC refuses those with "
            "a 403 regardless of whether the address is well formed",
            BANNED,
        )
        return 1
    if env.optional("SEC_CONTACT_EMAIL"):
        # The two are deliberately separate variables. Say so here rather
        # than leaving someone to wonder why one address was not enough.
        logger.info(
            "SEC_CONTACT_EMAIL is set but EDGAR_CONTACT_EMAIL is not. They are "
            "deliberately separate: setting a contact address for the quarterly "
            "universe refresh should not start a daily crawler"
        )
    # Not an error. An unset contact address is how this is switched off, and
    # exiting zero is what `screener.reddit` does with an empty subreddit list.
    # `restart: unless-stopped` restarts on any exit code, so the container
    # comes back, reads Infisical again and exits again -- a cheap loop, one
    # Infisical read a restart and nothing sent to SEC or the database -- and
    # setting the address in Infisical is all it takes to turn it back on,
    # because the next of those restarts reads it.
    # Set in the compose file instead, it would need the container recreated,
    # because `environment:` is fixed at create time.
    logger.info("EDGAR_CONTACT_EMAIL is not set; not ingesting insider transactions")
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    try:
        load_into_environ()
    except SecretsError as exc:
        logger.error("%s", exc)
        return 1

    config = EdgarConfig.from_env()

    if sys.argv[1:]:
        logger.error("%s", USAGE)
        return 1

    refused = _refusal(config)
    if refused is not None:
        return refused

    def stop(*_: Any) -> None:
        logger.info("stopping after the current pass")
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    logger.info(
        "ingesting form 4 filings every %dh, %d day window, %d day(s) a pass",
        config.refresh_hours, config.backfill_days, config.days_per_pass,
    )
    watch()

    while not stopping.is_set():
        # Read again every pass rather than once at boot. `watch()` keeps the
        # environment in step with Infisical, and a pass runs on what it holds
        # now: a new address is the one SEC sees next, and an unset one stops
        # the walk here.
        config = EdgarConfig.from_env()
        refused = _refusal(config)
        if refused is not None:
            return refused
        interval = config.refresh_hours * 3600
        # A pass that failed should not wait the full cycle. The usual cause is
        # transient -- SEC having a moment, or the schema not applied yet on a
        # first boot -- and twelve hours is a long time to sit on either.
        retry = min(300, interval)
        wait = interval
        try:
            once(config)
        except Exception as exc:
            # Logged and retried rather than fatal: under `restart:
            # unless-stopped` a hard exit is a loop hammering a government
            # service that blocks addresses which do it.
            logger.error("ingest pass failed, retrying in %ds: %s", retry, exc)
            wait = retry
        stopping.wait(wait)

    logger.info("edgar ingest stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
