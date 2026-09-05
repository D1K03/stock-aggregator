"""Entry point, shaped like `screener.universe` so the two read the same.

`prices` is the nightly path and works out what it needs per security. `sweep`
is a hand-run diagnostic that writes nothing.
"""

import argparse
import logging
from datetime import date

import psycopg

from screener.blobs import store
from screener.config import settings
from screener.ingest.chart import ChartClient
from screener.ingest.run import active_securities, run_prices
from screener.ingest.sweep import run_sweep
from screener.secrets import SecretsError, load_into_environ

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="screener.ingest", description=__doc__)
    parser.add_argument(
        "command",
        choices=("prices", "sweep"),
        help=(
            "prices: fetch missing daily bars per security, backfilling to 2020 "
            "on first sight. sweep: compare six years against what is stored "
            "and report differences, writing nothing."
        ),
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help=(
            "seconds between securities. Zero by default: 3,012 sequential "
            "requests produced no 429, and lanes already park an exit that "
            "rate-limits. This is the safeguard, not a known requirement."
        ),
    )
    parser.add_argument("--today", type=date.fromisoformat, default=None,
                        help="override the run date (testing); never in the past")
    parser.add_argument("--limit", type=int, default=0,
                        help="only the first N securities, for a smoke run")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    today = args.today or date.today()

    if today < date.today():
        # `--today` moves the settling cutoff with it, so a past date puts every
        # bar Yahoo returns on the upsert path and rewrites rows that had long
        # since settled. D3 exists to keep the permission to overwrite from
        # travelling with the fetch window; a backdated run would hand it over
        # entirely. Backfilling needs no flag anyway — a security with no rows
        # gets 2020-01-01 from `windows()`.
        logger.error(
            "--today %s is in the past; it moves the settling window with it and "
            "would rewrite settled bars. Backfill needs no flag.",
            today,
        )
        return 1

    try:
        # Same as `screener.boot`: the payload store's credentials, the database
        # URL and the proxy lanes all arrive this way in production. Without it
        # a VPS run falls back to whatever the container image happens to carry
        # — for the payload store, a `local` directory the next rollout deletes,
        # while `ingest_observation.blob_path` records that the evidence exists.
        load_into_environ()
    except SecretsError as exc:
        # Fatal on purpose, for the same reason as boot: half a configuration
        # means failing later, somewhere less obvious, against a database or a
        # bucket that may be the wrong one.
        logger.error("%s", exc)
        return 1

    # Autocommit: run_prices opens a `conn.transaction()` per security, and on a
    # non-autocommit connection that becomes a savepoint inside one long transaction
    # rather than its own commit boundary — nothing would persist until the whole
    # run ended, so a failure partway through would discard every security that had
    # already succeeded, and the per-security window derivation that makes failures
    # self-healing would have nothing on disk to heal from.
    with psycopg.connect(settings().database_url, autocommit=True) as conn:
        securities = active_securities(conn)
        if args.limit:
            securities = securities[: args.limit]
        if not securities:
            logger.error("no active securities; run `python -m screener.universe load` first")
            return 1

        with ChartClient() as client:
            if args.command == "prices":
                report = run_prices(
                    conn,
                    client=client,
                    blobs=store(),
                    today=today,
                    securities=securities,
                    delay=args.delay,
                )
                # The two change counts are reported separately: a
                # settling-window upsert and a corporate action that differs
                # from what is held are evidence for two different open
                # decisions, and one headline number answers neither.
                logger.info(
                    "prices: %d requested, %d ok, %d failed, "
                    "%d settling-window bar changes, %d corporate-action differences",
                    report.requested, report.ok, report.failed,
                    len(report.bar_changes), len(report.action_changes),
                )
                return 0 if report.ok else 1

            report = run_sweep(conn, client=client, today=today, securities=securities)
            logger.info(
                "sweep: %d compared, %d mismatches, by field %s",
                report.compared, len(report.mismatches), report.by_field or "none",
            )
            return 0
