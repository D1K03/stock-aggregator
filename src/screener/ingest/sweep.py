"""Does Yahoo actually correct settled bars, and how often?

Detect-only. Writes nothing: no rows, no blobs, no `ingest_observation`. It is a
diagnostic, not an observation of record, so it must not enter the trail.

The **field** matters more than the count. "Volume was revised" and "close was
revised" have completely different implications for a momentum score: if the
answer is a handful a year and all volume, `price_daily` keeps its snapshot key.
If closes are corrected regularly it needs `observed_at` in that key and a
point-in-time read, and that has to be known before any backtest means anything.
"""

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

import psycopg

from screener.ingest.load import BAR_FIELDS, Change
from screener.ingest.parse import parse
from screener.ingest.window import BACKFILL_START

logger = logging.getLogger(__name__)


@dataclass
class SweepReport:
    compared: int = 0
    mismatches: list[Change] = field(default_factory=list)
    by_field: dict[str, int] = field(default_factory=dict)


def run_sweep(
    conn: psycopg.Connection,
    *,
    client,
    today: date,
    securities: list[tuple[int, str]],
) -> SweepReport:
    report = SweepReport()
    counts: Counter[str] = Counter()

    for security_id, symbol in securities:
        payload = client.fetch(symbol, BACKFILL_START, today)
        if payload is None:
            logger.warning("sweep: no chart for %s", symbol)
            continue
        bars, _ = parse(payload)

        with conn.cursor() as cur:
            cur.execute(
                """select trade_date, open, high, low, close, volume
                     from price_daily where security_id = %s""",
                (security_id,),
            )
            held = {row[0]: row[1:] for row in cur.fetchall()}

        for bar in bars:
            existing = held.get(bar.trade_date)
            if existing is None:
                # We simply do not hold it. That is a gap, not a correction.
                continue
            report.compared += 1
            for name, old in zip(BAR_FIELDS, existing):
                new = getattr(bar, name)
                if old != new:
                    counts[name] += 1
                    report.mismatches.append(
                        Change(security_id, bar.trade_date, name, old, new)
                    )
                    logger.info(
                        "sweep mismatch: security=%s %s %s %s -> %s",
                        security_id, bar.trade_date, name, old, new,
                    )

    report.by_field = dict(counts)
    logger.info(
        "sweep: compared %d bars, %d mismatches, by field: %s",
        report.compared, len(report.mismatches), report.by_field or "none",
    )
    return report
