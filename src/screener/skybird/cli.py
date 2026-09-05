"""Entry point, shaped like `screener.boot` and `screener.universe` so the three
read the same.

`run` is what the container does and the only subcommand that captures
anything. The rest are the same writes the dashboard makes, available from a
terminal — which is how a capture gets started on a box with no browser, and how
one gets deleted when the dashboard is the thing that is broken.
"""

import argparse
import logging
import sys

import psycopg

from screener.config import settings
from screener.skybird import store, supervisor
from screener.skybird.config import SkybirdConfig
from screener.skybird.platforms import UnsupportedPlatform, resolve

logger = logging.getLogger(__name__)

# Who a row says asked for it, when nobody signed in did. The audit trail uses
# the same word for the same reason.
CLI_ACTOR = "system"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="screener.skybird", description=__doc__)
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=("run", "start", "stop", "list", "delete"),
        help="run: supervise captures (the container's command). "
             "start URL / stop ID / delete ID / list: the dashboard's writes, "
             "from a terminal.",
    )
    parser.add_argument("target", nargs="?", help="a stream URL, or a session id")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    config = SkybirdConfig.from_env()
    if args.command == "run":
        return supervisor.run(config)

    if args.command in {"start", "stop", "delete"} and not args.target:
        parser.error(f"{args.command} needs a {'URL' if args.command == 'start' else 'session id'}")

    with psycopg.connect(settings().database_url, autocommit=True) as conn:
        if args.command == "start":
            try:
                ref = resolve(args.target, parents=config.embed_parents)
            except UnsupportedPlatform as exc:
                logger.error("%s", exc)
                return 1
            try:
                session = store.create(
                    conn,
                    ref,
                    requested_by=CLI_ACTOR,
                    chunk_seconds=config.chunk_seconds,
                )
            except store.AlreadyLive as exc:
                logger.error("%s", exc)
                return 1
            logger.info("session %d requested: %s", session.id, session.source_url)
            return 0

        if args.command == "list":
            for session in store.listing(conn):
                logger.info(
                    "%5d  %-8s %-9s %4d segments  %s",
                    session.id,
                    session.platform,
                    session.state,
                    session.segment_count,
                    session.title or session.source_url,
                )
            return 0

        session_id = _as_id(args.target, parser)
        if args.command == "stop":
            if not store.request_stop(conn, session_id):
                logger.error("session %d is not running", session_id)
                return 1
            logger.info("session %d asked to stop", session_id)
            return 0

        if not store.delete(conn, session_id):
            logger.error("no session %d", session_id)
            return 1
        logger.info("session %d deleted, transcript and all", session_id)
        return 0


def _as_id(raw: str, parser: argparse.ArgumentParser) -> int:
    try:
        return int(raw)
    except ValueError:
        parser.error(f"{raw!r} is not a session id")


if __name__ == "__main__":
    sys.exit(main())
