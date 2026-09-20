"""`python -m screener.rupert` — the resolver container's entry point.

Wakes every `RUPERT_REFRESH_HOURS`, reads the next slice of each corpus past the
frontier, and stops when the day's call budget is spent.

No subcommands. Reading further back is a matter of how far the frontier has
got, and the only thing that moves it backwards is deleting a `rupert.progress`
row -- which is a deliberate act on a table anybody can see, and cheaper to
explain than a `backfill` that races the running container for the same rows.
Widening `RUPERT_DAILY_MAX_CALLS` is what makes a backlog drain faster, exactly
as widening `EDGAR_BACKFILL_DAYS` *is* edgar's backfill.

The wait is a `threading.Event`, not `time.sleep`. A signal handler cannot
interrupt a six-hour sleep, so SIGTERM would be answered whenever it happened to
finish and every deploy would sit through the full SIGKILL timeout. This is PID
1 in the container.
"""

import logging
import signal
import sys
import threading
from typing import Any

from screener.rupert.config import RupertConfig
from screener.rupert.run import once
from screener.secrets import SecretsError, load_into_environ

logger = logging.getLogger(__name__)

USAGE = "usage: python -m screener.rupert"

stopping = threading.Event()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    try:
        load_into_environ()
    except SecretsError as exc:
        logger.error("%s", exc)
        return 1

    if sys.argv[1:]:
        logger.error("%s", USAGE)
        return 1

    config = RupertConfig.from_env()
    if not config.enabled:
        # Not an error. A zero call budget is how this is switched off, and
        # exiting zero lets `restart: unless-stopped` leave it alone -- the
        # shape `screener.edgar` uses for an unset contact address.
        #
        # Said with the variable's name in it, because this layer costs money
        # and "off by default" should be discoverable from one log line rather
        # than from reading the config module.
        logger.info("RUPERT_DAILY_MAX_CALLS is 0; nothing to resolve")
        return 0

    def stop(*_: Any) -> None:
        logger.info("stopping after the current pass")
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    interval = config.refresh_hours * 3600
    # A pass that failed should not wait the full cycle. The usual cause is
    # transient -- the endpoint having a moment, or the schema not applied yet
    # on a first boot -- and six hours is a long time to sit on either.
    retry = min(300, interval)
    logger.info(
        "resolving every %dh, up to %d decisions a day, %d items a pass",
        config.refresh_hours, config.daily_max_calls, config.batch,
    )

    while not stopping.is_set():
        wait = interval
        try:
            once(config)
        except Exception as exc:
            # Logged and retried rather than fatal: under `restart:
            # unless-stopped` a hard exit is a loop, and this one would be a
            # loop that spends money every time round.
            logger.error("resolve pass failed, retrying in %ds: %s", retry, exc)
            wait = retry
        stopping.wait(wait)

    logger.info("rupert stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
