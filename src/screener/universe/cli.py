"""Entry point, shaped like `screener.boot` so the two read the same.

`refresh` talks to the network and never opens a database connection. `load`
talks to the database and never opens a socket. Keeping that split visible at
the command line is the point: the fragile, slow half runs quarterly by hand,
and its output is a committed file you can review before it moves a score.
"""

import argparse
import logging
from datetime import date
from pathlib import Path

from screener.universe.load import DepartureCeilingExceeded, load
from screener.universe.refresh import refresh

logger = logging.getLogger(__name__)

DEFAULT_DATA_DIR = Path("data")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="screener.universe", description=__doc__)
    parser.add_argument(
        "command",
        choices=("refresh", "load"),
        help=(
            "refresh: rebuild the universe CSV from Wikipedia, SEC and Yahoo "
            "(no database). load: reconcile that CSV into Postgres (no network)."
        ),
    )
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                        help="where universe.csv lives (default: data/)")
    parser.add_argument("--dry-run", action="store_true",
                        help="load: print the plan and change nothing")
    parser.add_argument("--force", action="store_true",
                        help="load: proceed past the departure ceiling")
    parser.add_argument("--as-of", type=date.fromisoformat, default=None,
                        help="load: date to stamp changes with (default: today)")
    parser.add_argument("--delay", type=float, default=0.0,
                        help="refresh: seconds to pause between symbols")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.command == "refresh":
        report = refresh(args.data_dir, delay=args.delay)
        logger.info("refresh: %d written, %d unresolved", report.written, report.unresolved)
        return 0

    try:
        result = load(
            args.data_dir / "universe.csv",
            as_of=args.as_of or date.today(),
            dry_run=args.dry_run,
            force=args.force,
        )
    except DepartureCeilingExceeded as exc:
        logger.error("%s", exc)
        return 1
    logger.info("load: %s", result.summary())
    return 0
