"""One night of scoring: the run's lifecycle, its reads and every write.

Deliberately unlike ingest, which commits per security. Here a half-scored day
is worse than no day -- tomorrow's crossing diff would compare against it and
invent a crossing for every security that never got scored -- so the whole run
is one transaction (spec D9). Volume makes that free: roughly 9,000 rows a
night against ingest's 2.4 million.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import psycopg

from screener.provenance import config_hash, require_git_sha
from screener.scoring.adjust import Action, adjusted_closes
from screener.scoring.blend import blend
from screener.scoring.metrics import CODES, compute, months_before
from screener.scoring.peers import MIN_PEERS, resolve
from screener.scoring.percentile import deciles, percentiles
from screener.scoring.pillars import score_pillar

logger = logging.getLogger(__name__)

# A live run scoring D at 02:00 the next morning needs an offset past that
# fetch. The value is stamped on the run and covered by `config_hash`, so
# changing it is visible in the run row rather than only in a deploy.
CUTOFF_OFFSET = timedelta(days=1, hours=6)

# Twelve months for `ret_12m`, plus a month of slack so the nearest bar at or
# before the target is inside the window rather than just outside it.
BAR_WINDOW_MONTHS = 13


def visibility_cutoff(as_of: date, cutoff_offset: timedelta) -> datetime:
    """The instant after which a fact is not visible to this scoring date."""
    return datetime.combine(as_of, datetime.min.time(), tzinfo=timezone.utc) + cutoff_offset


def active_securities(conn: psycopg.Connection) -> list[int]:
    """Every active security's id.

    Ids only, unlike `screener.ingest.active_securities`, which needs the
    current symbol because it is about to fetch one. Scoring never names a
    security to anything outside the database, so importing that function to
    throw half of it away would couple the two cycles for nothing.
    """
    with conn.cursor() as cur:
        cur.execute("select id from security where is_active order by id")
        return [row[0] for row in cur.fetchall()]


def read_bars(
    conn: psycopg.Connection,
    security_ids: Sequence[int],
    *,
    as_of: date,
    cutoff_offset: timedelta,
) -> dict[int, list[tuple[date, Decimal]]]:
    """Visible closes per security, ascending. One query for the whole night."""
    if not security_ids:
        return {}
    out: dict[int, list[tuple[date, Decimal]]] = {}
    with conn.cursor() as cur:
        cur.execute(
            """select security_id, trade_date, close
                 from price_daily
                where security_id = any(%(ids)s)
                  and trade_date > %(start)s
                  and trade_date <= %(as_of)s
                  and observed_at <= %(cutoff)s
             order by security_id, trade_date""",
            {
                "ids": list(security_ids),
                "start": months_before(as_of, BAR_WINDOW_MONTHS),
                "as_of": as_of,
                "cutoff": visibility_cutoff(as_of, cutoff_offset),
            },
        )
        for security_id, trade_date, close in cur.fetchall():
            out.setdefault(security_id, []).append((trade_date, close))
    return out


def read_actions(
    conn: psycopg.Connection,
    security_ids: Sequence[int],
    *,
    as_of: date,
    cutoff_offset: timedelta,
) -> dict[int, list[Action]]:
    """Visible splits and dividends over the same window as the bars."""
    if not security_ids:
        return {}
    out: dict[int, list[Action]] = {}
    with conn.cursor() as cur:
        cur.execute(
            """select security_id, effective_date, action_type, ratio, amount
                 from corporate_action
                where security_id = any(%(ids)s)
                  and effective_date > %(start)s
                  and effective_date <= %(as_of)s
                  and observed_at <= %(cutoff)s
             order by security_id, effective_date""",
            {
                "ids": list(security_ids),
                "start": months_before(as_of, BAR_WINDOW_MONTHS),
                "as_of": as_of,
                "cutoff": visibility_cutoff(as_of, cutoff_offset),
            },
        )
        for security_id, effective_date, action_type, ratio, amount in cur.fetchall():
            out.setdefault(security_id, []).append(
                Action(effective_date, action_type, ratio, amount)
            )
    return out


# Shared with `migrations/019_scoring_reference.sql` by hand, because
# `scoring_logic_version` has no code column to key on.
LOGIC_DESCRIPTION = "v1 momentum: four price metrics, sector percentiles"
WEIGHT_CODE = "v1"
PILLAR_CODE = "momentum"


class NoBarsVisible(RuntimeError):
    """No security had a visible bar, so there is nothing honest to write."""


@dataclass(frozen=True)
class Reference:
    logic_version_id: int
    weight_version_id: int
    pillar_id: int
    metric_ids: dict[str, int]
    higher_is_better: dict[str, bool]
    weights: dict[str, Decimal]


@dataclass(frozen=True)
class ScoringReport:
    as_of: date
    run_id: int
    scored: int
    skipped: int
    groups: int


def reference(conn: psycopg.Connection) -> Reference:
    """The seeded rows this run's numbers are stamped against."""
    with conn.cursor() as cur:
        cur.execute(
            "select id from scoring_logic_version where description = %s",
            (LOGIC_DESCRIPTION,),
        )
        logic = cur.fetchone()
        cur.execute("select id from weight_version where code = %s", (WEIGHT_CODE,))
        weight = cur.fetchone()
        cur.execute("select id from pillar where code = %s", (PILLAR_CODE,))
        pillar = cur.fetchone()
        if logic is None or weight is None or pillar is None:
            raise RuntimeError(
                "scoring reference data is missing; apply migrations with "
                "`python -m screener.boot migrate`"
            )
        cur.execute(
            "select code, id, higher_is_better from metric where code = any(%s)",
            (list(CODES),),
        )
        metrics = cur.fetchall()
        cur.execute(
            """select p.code, w.weight
                 from pillar_weight w
                 join pillar p on p.id = w.pillar_id
                where w.weight_version_id = %s""",
            (weight[0],),
        )
        weights = {row[0]: row[1] for row in cur.fetchall()}

    if len(metrics) != len(CODES):
        raise RuntimeError(
            f"expected {len(CODES)} seeded metrics, found {len(metrics)}"
        )
    return Reference(
        logic_version_id=logic[0],
        weight_version_id=weight[0],
        pillar_id=pillar[0],
        metric_ids={code: metric_id for code, metric_id, _ in metrics},
        higher_is_better={code: flag for code, _, flag in metrics},
        weights=weights,
    )


def open_run(
    conn: psycopg.Connection, *, as_of: date, cutoff_offset: timedelta
) -> int:
    """Insert the run row and return its id.

    Committed before the writes begin, and deliberately outside their
    transaction: a run that dies has to leave the row behind saying so.

    `config_hash` covers the *scoring* parameters this run's output depends on
    -- never process configuration, whose churn would break comparability
    between two otherwise identical runs.
    """
    ref = reference(conn)
    digest = config_hash(
        {
            "cutoff_offset": str(cutoff_offset),
            "min_peers": MIN_PEERS,
            "metrics": list(CODES),
            "bar_window_months": BAR_WINDOW_MONTHS,
        }
    )
    with conn.cursor() as cur:
        cur.execute(
            """insert into scoring_run
               (as_of_range, cutoff_offset, logic_version_id, weight_version_id,
                status, emits_alerts, git_sha, config_hash, started_at, outcome)
               values (daterange(%s, %s, '[)'), %s, %s, %s,
                       'live', false, %s, %s, now(), 'running')
               returning id""",
            (
                as_of,
                as_of + timedelta(days=1),
                cutoff_offset,
                ref.logic_version_id,
                ref.weight_version_id,
                # Strict, not lenient: this table is append-only and the column
                # exists for reproducibility, so a row stamped "unknown" is a
                # permanent, unrepairable lie about a run.
                require_git_sha(),
                digest,
            ),
        )
        row = cur.fetchone()
        assert row is not None
        return row[0]


def close_run(conn: psycopg.Connection, run_id: int, outcome: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "update scoring_run set finished_at = now(), outcome = %s where id = %s",
            (outcome, run_id),
        )


def write_metrics(cur: psycopg.Cursor, rows: Sequence[tuple]) -> None:
    cur.executemany(
        """insert into metric_daily
           (as_of, scoring_run_id, security_id, metric_id, raw_value, percentile,
            peer_group_id, peer_count, fallback_level)
           values (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        rows,
    )


def write_peer_group_stats(cur: psycopg.Cursor, rows: Sequence[tuple]) -> None:
    cur.executemany(
        """insert into peer_group_stat
           (as_of, scoring_run_id, peer_group_id, metric_id, member_count, deciles)
           values (%s, %s, %s, %s, %s, %s)""",
        rows,
    )


def write_pillar_scores(cur: psycopg.Cursor, rows: Sequence[tuple]) -> None:
    cur.executemany(
        """insert into pillar_score_daily
           (as_of, scoring_run_id, security_id, pillar_id, score, metric_count,
            coverage)
           values (%s, %s, %s, %s, %s, %s, %s)""",
        rows,
    )


def write_snapshots(cur: psycopg.Cursor, rows: Sequence[tuple]) -> None:
    cur.executemany(
        """insert into snapshot_daily
           (as_of, scoring_run_id, security_id, blended_score, pillar_agreement,
            min_coverage, worst_fallback_level)
           values (%s, %s, %s, %s, %s, %s, %s)""",
        rows,
    )


def score(
    conn: psycopg.Connection,
    *,
    run_id: int,
    as_of: date,
    cutoff_offset: timedelta,
) -> ScoringReport:
    """Compute and write one night. Runs inside the caller's transaction."""
    ref = reference(conn)
    securities = active_securities(conn)
    bars = read_bars(conn, securities, as_of=as_of, cutoff_offset=cutoff_offset)
    if not bars:
        raise NoBarsVisible(
            f"no bars visible for {as_of} under a {cutoff_offset} cutoff; "
            "an empty snapshot is worse than none"
        )
    actions = read_actions(conn, securities, as_of=as_of, cutoff_offset=cutoff_offset)

    raw: dict[int, dict[str, Decimal]] = {}
    for security_id in securities:
        series = adjusted_closes(bars.get(security_id, []), actions.get(security_id, []))
        values = compute(series, as_of)
        if values:
            raw[security_id] = values

    peers = resolve(conn, list(raw), as_of=as_of)

    # Grouped by (metric, peer group), because a percentile is only defined
    # within one of those.
    buckets: dict[tuple[str, int], list[tuple[int, Decimal]]] = {}
    for security_id, values in raw.items():
        group = peers[security_id].peer_group_id
        for code, value in values.items():
            buckets.setdefault((code, group), []).append((security_id, value))

    metric_rows: list[tuple] = []
    stat_rows: list[tuple] = []
    scored_percentiles: dict[int, dict[str, Decimal]] = {}
    for (code, group_id), members in buckets.items():
        values = [value for _, value in members]
        ranked = percentiles(values, higher_is_better=ref.higher_is_better[code])
        for (security_id, value), percentile in zip(members, ranked):
            metric_rows.append(
                (
                    as_of, run_id, security_id, ref.metric_ids[code], value,
                    percentile, group_id,
                    # The peers that actually produced this metric today, not
                    # everything the group holds: a percentile is worth exactly
                    # the number of values it was computed from.
                    len(members),
                    peers[security_id].level,
                )
            )
            scored_percentiles.setdefault(security_id, {})[code] = percentile
        stat_rows.append(
            (as_of, run_id, group_id, ref.metric_ids[code], len(members), deciles(values))
        )

    pillar_rows: list[tuple] = []
    snapshot_rows: list[tuple] = []
    for security_id, ranked in scored_percentiles.items():
        pillar = score_pillar(ranked, expected=len(CODES))
        if pillar is None:
            continue
        pillar_rows.append(
            (
                as_of, run_id, security_id, ref.pillar_id,
                pillar.score, pillar.metric_count, pillar.coverage,
            )
        )
        snapshot = blend(
            {PILLAR_CODE: pillar},
            ref.weights,
            [peers[security_id].level] * len(ranked),
        )
        if snapshot is None:
            continue
        snapshot_rows.append(
            (
                as_of, run_id, security_id, snapshot.blended_score,
                snapshot.pillar_agreement, snapshot.min_coverage,
                snapshot.worst_fallback_level,
            )
        )

    with conn.cursor() as cur:
        write_metrics(cur, metric_rows)
        write_peer_group_stats(cur, stat_rows)
        write_pillar_scores(cur, pillar_rows)
        write_snapshots(cur, snapshot_rows)

    return ScoringReport(
        as_of=as_of,
        run_id=run_id,
        scored=len(snapshot_rows),
        skipped=len(securities) - len(raw),
        groups=len({group for _, group in buckets}),
    )


def run_scoring(
    conn: psycopg.Connection,
    *,
    as_of: date,
    cutoff_offset: timedelta = CUTOFF_OFFSET,
) -> ScoringReport:
    """One night, on an autocommit connection.

    The run row is committed first so that a failure leaves it `running` --
    the record that a night died, exactly as ingest leaves `ingest_run`. The
    writes are one transaction beneath it.
    """
    run_id = open_run(conn, as_of=as_of, cutoff_offset=cutoff_offset)
    with conn.transaction():
        report = score(conn, run_id=run_id, as_of=as_of, cutoff_offset=cutoff_offset)
    close_run(conn, run_id, "ok")
    logger.info(
        "scored %d securities for %s across %d peer groups, %d skipped",
        report.scored, as_of, report.groups, report.skipped,
    )
    return report
