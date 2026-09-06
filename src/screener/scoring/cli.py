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
from screener.scoring.run import CUTOFF_OFFSET, NoBarsVisible, run_scoring
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


def _recovery_sql(as_of: date) -> str:
    # The exclusion constraint keys on `status`, not `outcome`: a run that
    # dies leaves its row at `status='live', outcome='running'` as the record
    # that the night died, and that row alone blocks every future attempt to
    # open a live run for the same date. A failed run's writes rolled back
    # whole, so the row has no dependents and deleting it is safe -- this is
    # named at the moment of failure so an operator at 02:00 has a sanctioned
    # action instead of a permanently wedged date.
    upper = (as_of + timedelta(days=1)).isoformat()
    return (
        f"the run holding {as_of} wrote nothing and still holds the date as live;\n"
        f"release it with:\n"
        f"  delete from scoring_run\n"
        f"   where as_of_range = daterange('{as_of}', '{upper}', '[)')\n"
        f"     and outcome = 'running'"
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
        except NoBarsVisible as exc:
            logger.error("%s\n%s", exc, _recovery_sql(as_of))
            return 1
        except psycopg.errors.ExclusionViolation:
            # Exactly what an operator hits on the *second* attempt after a
            # failed night: the stale row from the first attempt is still
            # `status='live'`, so telling them how to clear it is the point.
            logger.error(
                "a live run already covers %s; supersede it rather than scoring "
                "the date twice\n%s",
                as_of, _recovery_sql(as_of),
            )
            return 1
        except Exception:
            # `open_run` commits before `score()` runs inside its own
            # transaction, so anything else `score()` raises -- a write
            # constraint violation, a transient database error, a bug -- still
            # leaves the run row behind as a wedged date, exactly like
            # `NoBarsVisible`. Catching `Exception` (never a bare `except`,
            # and never `SystemExit`/`KeyboardInterrupt`) means this is the
            # backstop for whatever the two named branches above do not
            # already cover, not a replacement for them. `logger.exception`
            # keeps the traceback in the log for whoever debugs the bug
            # itself; returning 1 rather than re-raising matches the other
            # branches so the process always exits cleanly with a recovery
            # instruction rather than a bare traceback and no next step.
            logger.exception(
                "scoring %s failed unexpectedly\n%s", as_of, _recovery_sql(as_of),
            )
            return 1
    logger.info(
        "run %d: %d scored, %d skipped, %d peer groups",
        report.run_id, report.scored, report.skipped, report.groups,
    )
    return 0
