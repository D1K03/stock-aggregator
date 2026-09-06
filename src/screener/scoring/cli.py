"""Entry point, shaped like `screener.ingest` so the two read the same.

One command. The diff, the cooldown and the Discord POST belong to the
alerting cycle, and `emits_alerts` is false precisely so that cycle can be
built deliberately rather than inherited.
"""

import argparse
import logging
from datetime import date, timedelta

import psycopg

from screener.config import settings
from screener.scoring.run import (
    CUTOFF_OFFSET,
    NoBarsVisible,
    ScoringInProgress,
    run_scoring,
)
from screener.secrets import SecretsError, load_into_environ

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="screener.scoring", description=__doc__)
    parser.add_argument(
        "command",
        choices=("run",),
        help="run: score every active security for a date and write the snapshot",
    )
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=None,
        help="override the run date (testing); never in the past",
    )
    return parser


def _recovery_note(as_of: date) -> str:
    # Recovery is automatic now, and saying so is the point: before migration
    # 020 the exclusion constraint keyed on `status` alone, so the row a failed
    # night left behind blocked that date for ever and an operator had to
    # delete it by hand. `run_scoring` marks a catchable failure `failed` on
    # the way out, and `reconcile` settles anything a killed process could not.
    # What is left worth saying at 02:00 is which date is affected and that
    # re-running it is the whole of the fix.
    return (
        f"the run holding {as_of} wrote nothing; it is marked failed and no "
        f"longer holds the date.\n"
        f"re-run it with:\n"
        f"  python -m screener.scoring run --as-of {as_of}"
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    as_of = args.as_of or date.today()

    if as_of < date.today():
        # A live run may not overlap another live run's date, so a backdated
        # run is a constraint violation rather than a convenience. Historical
        # scores are `status='backfill'`, which this cycle does not build.
        logger.error(
            "--as-of %s is in the past; a live run may not cover a date another "
            "already covers. Historical scores are backfill runs, which do not "
            "exist yet.",
            as_of,
        )
        return 1

    try:
        load_into_environ()
    except SecretsError as exc:
        logger.error("%s", exc)
        return 1

    # Autocommit, so `open_run` commits before the writes begin and a failure
    # leaves the run row behind as the record that a night died.
    with psycopg.connect(settings().database_url, autocommit=True) as conn:
        try:
            report = run_scoring(conn, as_of=as_of, cutoff_offset=CUTOFF_OFFSET)
        except ScoringInProgress as exc:
            # Before the generic branch, and before the recovery note: no run
            # row was opened, nothing is wedged, and the note's "re-run it"
            # would be the wrong advice. Someone else is scoring; the answer
            # is to wait, not to try again immediately.
            logger.error("%s", exc)
            return 1
        except NoBarsVisible as exc:
            logger.error("%s\n%s", exc, _recovery_note(as_of))
            return 1
        except psycopg.errors.ExclusionViolation:
            # After 020 this no longer means a stale row from a failed
            # night -- those are marked `failed` and stop holding the date.
            # It means a run that genuinely stands: one that finished `ok`, or
            # one still in flight. Either way the answer is not to clear it.
            logger.error(
                "a live run already covers %s and has not failed; supersede it "
                "rather than scoring the date twice",
                as_of,
            )
            return 1
        except Exception:
            # `open_run` commits before `score()` runs inside its own
            # transaction, so anything else `score()` raises -- a write
            # constraint violation, a transient database error, a bug -- still
            # leaves the run row behind, exactly like `NoBarsVisible`.
            # Catching `Exception` (never a bare `except`, and never
            # `SystemExit`/`KeyboardInterrupt`) means this is the backstop for
            # whatever the two named branches above do not already cover, not
            # a replacement for them. `logger.exception` keeps the traceback
            # in the log for whoever debugs the bug itself; returning 1 rather
            # than re-raising matches the other branches so the process always
            # exits cleanly with a next step rather than a bare traceback.
            logger.exception(
                "scoring %s failed unexpectedly\n%s", as_of, _recovery_note(as_of),
            )
            return 1
    logger.info(
        "run %d: %d scored, %d skipped, %d peer groups",
        report.run_id, report.scored, report.skipped, report.groups,
    )
    return 0
