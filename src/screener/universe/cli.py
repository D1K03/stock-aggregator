"""Entry point, shaped like `screener.boot` so the two read the same."""

import argparse
import logging

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="screener.universe", description=__doc__)
    parser.add_argument(
        "command",
        choices=("refresh", "load"),
        help=(
            "refresh: rebuild data/universe.csv from Wikipedia, SEC and Yahoo "
            "(no database). load: reconcile that CSV into Postgres (no network)."
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="load: print the plan, change nothing")
    parser.add_argument("--force", action="store_true", help="load: proceed past the departure ceiling")
    parser.add_argument("--as-of", default=None, help="load: date to stamp changes with (default today)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    raise NotImplementedError(args.command)
