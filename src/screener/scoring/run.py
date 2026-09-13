"""One night of scoring: the run's lifecycle, its reads and every write.

Deliberately unlike ingest, which commits per security. Here a half-scored day
is worse than no day -- tomorrow's crossing diff would compare against it and
invent a crossing for every security that never got scored -- so the whole run
is one transaction (spec D9). Volume makes that free: roughly 9,000 rows a
night against ingest's 2.4 million.
"""

import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import psycopg

# `screener.ingest` owns the point-in-time fact read, including the `period_type`
# rule in its `distinct on`. Importing it keeps one definition of what a scoring
# date may see of the fact layer; `ingest.load` imports scoring lazily for the same
# reason in the other direction, which is what keeps the pair cycle-free.
from screener.ingest import read_facts
from screener.provenance import config_hash, require_git_sha
from screener.scoring.adjust import Action, adjusted_closes
from screener.scoring.basis import (
    ANNUAL_MAX_AGE_DAYS,
    QUARTER_GAP_MAX_DAYS,
    QUARTER_GAP_MIN_DAYS,
    SPLIT_WINDOW_DAYS,
    TTM_MAX_AGE_DAYS,
    Item,
    index_facts,
)
from screener.scoring.blend import blend
from screener.scoring.metrics import CODES, compute, months_before
from screener.scoring.peers import MIN_PEERS, market_group, resolve
from screener.scoring.pillars import PillarScore, score_pillar
from screener.scoring.ranking import rank
from screener.scoring.ratios import (
    RATIO_CODES,
    TAX_RATE_CEILING,
    Ratio,
    applicable,
    compute_ratios,
)

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


def read_currencies(
    conn: psycopg.Connection, security_ids: Sequence[int]
) -> dict[int, str]:
    """Each security's trading currency: the one its facts must share (spec D9)."""
    if not security_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "select id, currency from security where id = any(%s)",
            (list(security_ids),),
        )
        return {row[0]: row[1] for row in cur.fetchall()}


# Shared with `migrations/022_ratios.sql` by hand, because
# `scoring_logic_version` has no code column to key on.
LOGIC_DESCRIPTION = (
    "v2 momentum, valuation, quality: sector percentiles, industry-applicable ratios"
)
WEIGHT_CODE = "v2"
MOMENTUM = "momentum"
PILLAR_CODES: tuple[str, ...] = (MOMENTUM, "valuation", "quality")


class NoBarsVisible(RuntimeError):
    """No security had a visible bar, so there is nothing honest to write."""


class ScoringInProgress(RuntimeError):
    """Another process holds the scoring lock, so this one does not start."""


# Distinct from `screener.boot`'s migration lock; any 64-bit constant will do,
# and it only has to be unique within this database.
#
# This exists to make `reconcile` safe rather than to serialise the nightly job
# for its own sake. Without a lock, a run row at 'running' is ambiguous -- a
# night still in flight looks exactly like one whose process is gone -- and
# reconciling would eventually declare a healthy run dead and let a second one
# score the same date beside it. Holding the lock is what earns the right to
# say "anything still 'running' belongs to nobody".
SCORING_LOCK_ID = 8_119_003


@dataclass(frozen=True)
class Reference:
    logic_version_id: int
    weight_version_id: int
    pillar_ids: dict[str, int]
    metric_ids: dict[str, int]
    metric_pillar: dict[str, str]
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
    wanted = [*CODES, *RATIO_CODES]
    with conn.cursor() as cur:
        cur.execute(
            "select id from scoring_logic_version where description = %s",
            (LOGIC_DESCRIPTION,),
        )
        logic = cur.fetchone()
        cur.execute("select id from weight_version where code = %s", (WEIGHT_CODE,))
        weight = cur.fetchone()
        if logic is None or weight is None:
            raise RuntimeError(
                "scoring reference data is missing; apply migrations with "
                "`python -m screener.boot migrate`"
            )
        cur.execute(
            "select code, id from pillar where code = any(%s)", (list(PILLAR_CODES),)
        )
        pillars = {row[0]: row[1] for row in cur.fetchall()}
        cur.execute(
            """select m.code, m.id, m.higher_is_better, p.code
                 from metric m join pillar p on p.id = m.pillar_id
                where m.code = any(%s)""",
            (wanted,),
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

    if len(pillars) != len(PILLAR_CODES):
        raise RuntimeError(
            f"expected pillars {', '.join(PILLAR_CODES)}; found {', '.join(sorted(pillars))}"
        )
    if len(metrics) != len(wanted):
        raise RuntimeError(
            f"expected {len(wanted)} seeded metrics, found {len(metrics)}"
        )
    if not any(weights.get(code, Decimal(0)) > 0 for code in PILLAR_CODES):
        # `blend` drops any pillar the weight version doesn't weight positively,
        # so a version weighting none of the computed pillars would write a full
        # `pillar_score_daily` and zero `snapshot_daily` rows -- the same
        # empty-snapshot night `NoBarsVisible` exists to prevent, arriving
        # through the weight version instead of the bars. Zeroing one pillar is
        # an ordinary weighting; zeroing all of them is not.
        raise RuntimeError(
            f"weight version {WEIGHT_CODE!r} weights none of "
            f"{', '.join(PILLAR_CODES)} positively; fix its `pillar_weight` rows "
            "before scoring with it"
        )
    return Reference(
        logic_version_id=logic[0],
        weight_version_id=weight[0],
        pillar_ids=pillars,
        metric_ids={code: metric_id for code, metric_id, _, _ in metrics},
        metric_pillar={code: pillar for code, _, _, pillar in metrics},
        higher_is_better={code: flag for code, _, flag, _ in metrics},
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
            "ratios": list(RATIO_CODES),
            "ttm_max_age_days": TTM_MAX_AGE_DAYS,
            "quarter_gap_days": [QUARTER_GAP_MIN_DAYS, QUARTER_GAP_MAX_DAYS],
            "annual_max_age_days": ANNUAL_MAX_AGE_DAYS,
            "split_window_days": SPLIT_WINDOW_DAYS,
            "tax_rate_ceiling": str(TAX_RATE_CEILING),
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
    # `fundamental_fact_id` is left null for every metric: one key cannot hold the
    # facts a ratio rests on, and traceability is by reproduction (ratios spec
    # D14). `period_end` and `period_basis` name the period instead.
    cur.executemany(
        """insert into metric_daily
           (as_of, scoring_run_id, security_id, metric_id, raw_value, percentile,
            peer_group_id, peer_count, fallback_level, period_end, period_basis)
           values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
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
    facts = read_facts(conn, securities, as_of=as_of, cutoff_offset=cutoff_offset)
    currencies = read_currencies(conn, securities)
    peers = resolve(conn, securities, as_of=as_of)

    values: dict[int, dict[str, Decimal]] = {}
    periods: dict[tuple[int, str], Ratio] = {}
    # How each applicable ratio was assembled tonight, so a Yahoo change -- a
    # series dropped, a sign convention flipped -- shows in the log as a falling
    # count before anyone queries for it (spec §7).
    tally: dict[str, Counter[str]] = {code: Counter() for code in RATIO_CODES}
    for security_id in securities:
        security_bars = bars.get(security_id, [])
        security_actions = actions.get(security_id, [])
        found = dict(compute(adjusted_closes(security_bars, security_actions), as_of))

        industry = peers[security_id].industry
        held = index_facts(
            (
                Item(f.metric_code, f.period_end, f.period_type, f.value, f.currency)
                for f in facts.get(security_id, [])
            ),
            currency=currencies[security_id],
        )
        ratios = compute_ratios(
            held,
            industry=industry,
            # The raw close of the latest visible bar: total-return adjustment is
            # anchored at the present, so it would not change this one (spec D8).
            close=security_bars[-1][1] if security_bars else None,
            split_dates=[
                action.effective_date
                for action in security_actions
                if action.action_type == "split"
            ],
            as_of=as_of,
        )
        for codes in applicable(industry).values():
            for code in codes:
                ratio = ratios.get(code)
                if ratio is None:
                    tally[code]["absent"] += 1
                else:
                    tally[code][ratio.basis or "date"] += 1
        for code, ratio in ratios.items():
            found[code] = ratio.value
            periods[(security_id, code)] = ratio

        if found:
            values[security_id] = found

    placed, stats = rank(
        values,
        {sid: (peers[sid].peer_group_id, peers[sid].level) for sid in values},
        market_id=market_group(conn),
        higher_is_better=ref.higher_is_better,
        min_peers=MIN_PEERS,
    )

    metric_rows: list[tuple] = []
    ranked: dict[int, dict[str, dict[str, Decimal]]] = {}
    levels: dict[int, list[int]] = {}
    for place in placed:
        ratio = periods.get((place.security_id, place.code))
        metric_rows.append(
            (
                as_of, run_id, place.security_id, ref.metric_ids[place.code],
                place.value, place.percentile, place.peer_group_id,
                # The peers that actually produced this metric today, not
                # everything the group holds: a percentile is worth exactly the
                # number of values it was computed from.
                place.peer_count,
                place.level,
                ratio.period_end if ratio is not None else None,
                ratio.basis if ratio is not None else None,
            )
        )
        pillar_code = ref.metric_pillar[place.code]
        ranked.setdefault(place.security_id, {}).setdefault(pillar_code, {})[
            place.code
        ] = place.percentile
        levels.setdefault(place.security_id, []).append(place.level)

    stat_rows = [
        (as_of, run_id, stat.peer_group_id, ref.metric_ids[stat.code], stat.member_count, stat.deciles)
        for stat in stats
    ]

    pillar_rows: list[tuple] = []
    snapshot_rows: list[tuple] = []
    for security_id, by_pillar in ranked.items():
        # Coverage's denominator is what applies to this security, so a bank whose
        # one applicable Quality ratio is present is fully covered (spec D10).
        expected = {MOMENTUM: len(CODES), **{
            pillar_code: len(codes)
            for pillar_code, codes in applicable(peers[security_id].industry).items()
        }}
        scored: dict[str, PillarScore] = {}
        for pillar_code, percentiles_by_code in by_pillar.items():
            pillar = score_pillar(percentiles_by_code, expected=expected[pillar_code])
            if pillar is None:
                continue
            scored[pillar_code] = pillar
            pillar_rows.append(
                (
                    as_of, run_id, security_id, ref.pillar_ids[pillar_code],
                    pillar.score, pillar.metric_count, pillar.coverage,
                )
            )
        snapshot = blend(scored, ref.weights, levels[security_id])
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

    for code in RATIO_CODES:
        counts = tally[code]
        if counts:
            logger.info(
                "%s: %d TTM, %d annual, %d at a date, %d absent",
                code, counts["TTM"], counts["A"], counts["date"], counts["absent"],
            )

    return ScoringReport(
        as_of=as_of,
        run_id=run_id,
        scored=len(snapshot_rows),
        skipped=len(securities) - len(values),
        groups=len({place.peer_group_id for place in placed}),
    )


def reconcile(conn: psycopg.Connection) -> int:
    """Settle runs left behind by a process that died without saying so.

    Runs once, under the scoring lock and before any run row is opened, so
    'running' at that moment can only mean a process that is no longer there --
    a live one would be holding the lock this caller just took. The same shape
    as `screener.skybird.store.reconcile`, for the same reason: a row nobody is
    behind has to be settled by whoever notices, because the thing that would
    have settled it is gone.

    Marked failed rather than deleted. The row is the record that a night
    happened and did not finish, which is worth keeping; what it must stop
    doing is holding the date, and after 020 `outcome = 'failed'` is exactly
    what stops that.
    """
    with conn.cursor() as cur:
        cur.execute(
            "update scoring_run set outcome = 'failed', finished_at = now() "
            "where outcome = 'running'"
        )
        settled = cur.rowcount
    if settled:
        logger.warning("reconciled %d scoring run(s) left by a previous process", settled)
    return settled


def run_scoring(
    conn: psycopg.Connection,
    *,
    as_of: date,
    cutoff_offset: timedelta = CUTOFF_OFFSET,
) -> ScoringReport:
    """One night, on an autocommit connection.

    The run row is committed before the writes begin, so a failure rolls the
    writes back whole and leaves the row behind as the record that the night
    died. Two things then keep that record from wedging the date: a failure we
    can catch marks itself `failed` on the way out, and one we cannot -- a
    kill, an OOM, a container going away -- is settled by `reconcile` at the
    start of the next run.
    """
    row = conn.execute(
        "select pg_try_advisory_lock(%s)", (SCORING_LOCK_ID,)
    ).fetchone()
    assert row is not None
    if not row[0]:
        raise ScoringInProgress(
            "another scoring run holds the lock; refusing to score the same "
            "night twice"
        )
    try:
        reconcile(conn)
        run_id = open_run(conn, as_of=as_of, cutoff_offset=cutoff_offset)
        try:
            with conn.transaction():
                report = score(
                    conn, run_id=run_id, as_of=as_of, cutoff_offset=cutoff_offset
                )
        except BaseException:
            # `BaseException`, not `Exception`: a run killed by a timeout or a
            # Ctrl-C has wedged its date exactly as thoroughly as one that hit
            # a bug, and the row costs one statement to settle while the
            # process is still alive to do it.
            try:
                close_run(conn, run_id, "failed")
            except psycopg.Error:
                # The connection itself is gone, which is the case `reconcile`
                # exists for. Say so rather than replacing the real failure
                # with the failure to record it.
                logger.warning(
                    "could not mark run %d failed; the next run reconciles it",
                    run_id,
                )
            raise
        close_run(conn, run_id, "ok")
    finally:
        # Session-scoped, so a killed process releases it when its socket
        # dies. This is for the ordinary path, where the process lives on to
        # do something else -- a lock held past the run would wedge tomorrow
        # night as surely as the constraint once wedged today.
        conn.execute("select pg_advisory_unlock(%s)", (SCORING_LOCK_ID,))
    logger.info(
        "scored %d securities for %s across %d peer groups, %d skipped",
        report.scored, as_of, report.groups, report.skipped,
    )
    return report
