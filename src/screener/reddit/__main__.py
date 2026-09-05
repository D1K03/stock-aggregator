"""`python -m screener.reddit` — the ingest container's entry point.

Wakes every `REDDIT_REFRESH_HOURS`, backfilling the first time it sees a
subreddit and keeping up after that.

The wait is a `threading.Event`, not `time.sleep`. A signal handler cannot
interrupt a six-hour sleep, so SIGTERM would be answered whenever it happened to
finish and every deploy would sit through the full SIGKILL timeout. This is PID
1 in the container, which is the same reason `health.serve` and
`transcribe.serve` handle their own shutdown rather than being torn down.
"""

import logging
import signal
import threading
from typing import Any

from screener.reddit.config import RedditConfig
from screener.reddit.ingest import once
from screener.secrets import SecretsError, load_into_environ

logger = logging.getLogger(__name__)

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

    config = RedditConfig.from_env()
    if not config.enabled:
        # Not an error. An empty subreddit list is how this is switched off, and
        # exiting zero lets `restart: unless-stopped` leave it alone.
        logger.info("REDDIT_SUBREDDITS is empty; nothing to ingest")
        return 0

    def stop(*_: Any) -> None:
        logger.info("stopping after the current pass")
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    interval = config.refresh_hours * 3600
    # A pass that failed should not wait the full cycle to try again. The usual
    # cause is transient — the mirror having a moment, or the schema not applied
    # yet on a first boot — and six hours is a long time to sit on either.
    retry = min(300, interval)
    logger.info(
        "ingesting %s every %dh, %d day backfill",
        ", ".join(config.subreddits), config.refresh_hours, config.backfill_days,
    )

    while not stopping.is_set():
        wait = interval
        try:
            once(config)
        except Exception as exc:
            # Logged and retried rather than fatal: under `restart:
            # unless-stopped` a hard exit is a loop hammering someone else's
            # service, and this one is run by volunteers.
            logger.error("ingest pass failed, retrying in %ds: %s", retry, exc)
            wait = retry
        stopping.wait(wait)

    logger.info("reddit ingest stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
