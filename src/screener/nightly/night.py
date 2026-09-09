"""One night: prices, then fundamentals, then scoring.

Ordering is by construction rather than by clock arithmetic. Three separately
triggered jobs would encode "scoring runs after ingest" as an assumption about
gaps between trigger times, which breaks the first evening ingest runs long.

Nothing here changes what a command does. These are the same three functions
`screener.ingest.cli` and `screener.scoring.cli` call.
"""

import logging
from dataclasses import dataclass
from datetime import date

import psycopg

from screener.blobs import BlobStore
from screener.ingest import (
    FundamentalsReport,
    IngestReport,
    active_securities,
    run_fundamentals,
    run_prices,
)
from screener.scoring import ScoringReport, run_scoring

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NightReport:
    prices: IngestReport
    fundamentals: FundamentalsReport
    # None when the gate stopped it. A night that did not score is a night that
    # did not happen, because the snapshot is the point.
    scoring: ScoringReport | None

    @property
    def ok(self) -> bool:
        return self.scoring is not None


def already_scored(conn: psycopg.Connection, day: date) -> bool:
    """Whether a live run has already scored `day` successfully.

    Half of the catch-up condition. `outcome = 'ok'` rather than merely the row
    existing: a run left `running` by a killed process, or marked `failed` on
    the way out, is a night still owed rather than one finished.
    """
    with conn.cursor() as cur:
        cur.execute(
            "select 1 from scoring_run "
            "where status = 'live' and outcome = 'ok' "
            "  and as_of_range @> %s::date "
            "limit 1",
            (day,),
        )
        return cur.fetchone() is not None


def run_night(
    conn: psycopg.Connection,
    *,
    today: date,
    blobs: BlobStore,
    chart,
    timeseries,
) -> NightReport:
    """Fetch, then score, on one autocommit connection.

    Autocommit because both ingest halves commit per security and `run_scoring`
    commits its run row before the writes it wraps -- all three already depend
    on it.
    """
    securities = active_securities(conn)
    prices = run_prices(
        conn, client=chart, blobs=blobs, today=today, securities=securities
    )
    fundamentals = run_fundamentals(
        conn, client=timeseries, blobs=blobs, today=today, securities=securities
    )

    if prices.status == "failed":
        # `cutoff_offset` filters on `observed_at`, so yesterday's bars are
        # still visible: scoring now would write a complete-looking snapshot
        # from stale data, and tomorrow's crossing diff would read it as
        # "nothing moved". A silent-wrong shape, so the night stops here.
        logger.error(
            "prices wholly failed for %s (%d requested); not scoring",
            today, prices.requested,
        )
        return NightReport(prices, fundamentals, None)

    # Gated on prices alone, deliberately. Scoring reads bars and nothing else
    # this cycle -- no ratio consumes a fundamental fact yet -- so a
    # fundamentals failure must not block a run that does not depend on it.
    # **When the ratios cycle lands, this gate widens to fundamentals.**
    scoring = run_scoring(conn, as_of=today)
    return NightReport(prices, fundamentals, scoring)
