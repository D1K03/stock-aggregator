"""One night of scoring, end to end against a real database.

The fixture builds a sector large enough to clear the peer floor and gives
every security five sparse bars, which is enough for all four calendar windows
-- the metrics take the nearest bar at or before each target, so a dense series
would only make the test slower.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from screener.scoring import (
    CUTOFF_OFFSET,
    MIN_PEERS,
    NoBarsVisible,
    reference,
    run_scoring,
)

AS_OF = date(2026, 3, 2)
SEEN = datetime(2026, 1, 1, tzinfo=timezone.utc)
# 380 days back is inside `price_daily`'s 2025 partition and inside
# `read_bars`'s 13-month window (`BAR_WINDOW_MONTHS`), while still preceding
# every metric's target date -- 400 days back falls outside that window and
# silently drops `ret_12m` and `off_52w_high` for every security.
OFFSETS = (380, 200, 100, 30, 0)


@pytest.fixture
def universe(fresh_db, an_observation):
    """`MIN_PEERS` securities in one sector, each with five bars.

    Security i closes at 100 on every bar but the last, which closes at
    100 + i -- so the twelve-month returns are distinct and ordered, and the
    percentiles they produce are predictable.
    """
    with fresh_db.cursor() as cur:
        cur.execute(
            "insert into sector_scheme (code, name) values ('yfinance', 'yfinance')"
            " returning id"
        )
        scheme = cur.fetchone()[0]
        cur.execute(
            "insert into sector_node (scheme_id, level, code, name)"
            " values (%s, 1, 'technology', 'Technology') returning id",
            (scheme,),
        )
        sector = cur.fetchone()[0]
        cur.execute(
            "insert into sector_node (scheme_id, parent_id, level, code, name)"
            " values (%s, %s, 2, 'software', 'Software') returning id",
            (scheme, sector),
        )
        industry = cur.fetchone()[0]
        cur.execute(
            "insert into peer_group (scheme_id, sector_node_id, level, code)"
            " values (%s, null, 0, 'market') returning id",
            (scheme,),
        )
        market = cur.fetchone()[0]
        cur.execute(
            "insert into peer_group (scheme_id, sector_node_id, level, code)"
            " values (%s, %s, 1, 'technology') returning id",
            (scheme, sector),
        )
        sector_group = cur.fetchone()[0]

    ids = []
    for i in range(MIN_PEERS):
        security = fresh_db.execute(
            """insert into security
               (name, mic, currency, country, primary_symbol, first_seen)
               values (%s, 'XNAS', 'USD', 'US', %s, '2020-01-01') returning id""",
            (f"Co {i}", f"S{i:03d}"),
        ).fetchone()[0]
        ids.append(security)
        fresh_db.execute(
            """insert into security_sector
               (security_id, sector_node_id, valid_from, source)
               values (%s, %s, '2020-01-01', 'yfinance')""",
            (security, industry),
        )
        observation = an_observation(security)
        for offset in OFFSETS:
            close = Decimal(100 + i) if offset == 0 else Decimal(100)
            fresh_db.execute(
                """insert into price_daily
                   (security_id, trade_date, open, high, low, close, volume,
                    observed_at, ingest_observation_id)
                   values (%s, %s, %s, %s, %s, %s, 1, %s, %s)""",
                (
                    security,
                    AS_OF - timedelta(days=offset),
                    close, close, close, close,
                    SEEN,
                    observation,
                ),
            )
    return {"ids": ids, "market": market, "sector_group": sector_group}


# Four literal queries rather than one interpolated helper: psycopg types a
# query as LiteralString so that SQL assembled at runtime is rejected, and
# pyright checks `tests/` too.
def _counts(conn) -> dict[str, int]:
    return {
        "metric_daily": conn.execute(
            "select count(*) from metric_daily"
        ).fetchone()[0],
        "pillar_score_daily": conn.execute(
            "select count(*) from pillar_score_daily"
        ).fetchone()[0],
        "snapshot_daily": conn.execute(
            "select count(*) from snapshot_daily"
        ).fetchone()[0],
        "peer_group_stat": conn.execute(
            "select count(*) from peer_group_stat"
        ).fetchone()[0],
    }


def test_a_night_writes_all_four_derived_tables(fresh_db, universe):
    report = run_scoring(fresh_db, as_of=AS_OF)

    counts = _counts(fresh_db)

    assert report.scored == MIN_PEERS
    assert report.skipped == 0
    assert counts["metric_daily"] == MIN_PEERS * 4
    assert counts["pillar_score_daily"] == MIN_PEERS
    assert counts["snapshot_daily"] == MIN_PEERS
    # One row per (peer group, metric).
    assert counts["peer_group_stat"] == 4


def test_every_run_this_cycle_produces_has_alerting_switched_off(fresh_db, universe):
    run_scoring(fresh_db, as_of=AS_OF)

    rows = fresh_db.execute(
        "select emits_alerts, status, outcome from scoring_run"
    ).fetchall()

    assert rows == [(False, "live", "ok")]


def test_the_run_records_the_build_and_the_scoring_parameters(fresh_db, universe):
    run_scoring(fresh_db, as_of=AS_OF)

    git_sha, config_hash, cutoff = fresh_db.execute(
        "select git_sha, config_hash, cutoff_offset from scoring_run"
    ).fetchone()

    assert git_sha and git_sha != "unknown"
    assert len(bytes(config_hash)) == 32
    assert cutoff == CUTOFF_OFFSET


def test_raw_value_is_stored_beside_the_percentile(fresh_db, universe):
    # A score must trace back to visible raw inputs, so an alert can say
    # whether this security moved or its peers did.
    run_scoring(fresh_db, as_of=AS_OF)

    rows = fresh_db.execute(
        """select md.raw_value, md.percentile
             from metric_daily md join metric m on m.id = md.metric_id
            where m.code = 'ret_12m'
         order by md.raw_value"""
    ).fetchall()

    assert len(rows) == MIN_PEERS
    assert rows[0][1] == Decimal(0)
    assert rows[-1][1] == Decimal(100)
    assert rows[0][0] < rows[-1][0]


def test_the_blended_score_equals_the_momentum_pillar_score(fresh_db, universe):
    # Under {Momentum: 1.0} it can be nothing else, and saying so in a test is
    # what keeps the blend honest when a second pillar arrives.
    run_scoring(fresh_db, as_of=AS_OF)

    rows = fresh_db.execute(
        """select s.blended_score, p.score
             from snapshot_daily s
             join pillar_score_daily p
               on p.security_id = s.security_id and p.as_of = s.as_of"""
    ).fetchall()

    assert rows and all(blended == score for blended, score in rows)


def test_a_full_sector_scores_at_sector_level(fresh_db, universe):
    run_scoring(fresh_db, as_of=AS_OF)

    groups = fresh_db.execute(
        "select distinct peer_group_id, fallback_level from metric_daily"
    ).fetchall()

    assert groups == [(universe["sector_group"], 1)]


def test_a_security_short_of_every_window_is_absent_rather_than_zero(fresh_db, universe):
    # Spec D8: no metric rows, no pillar row, no snapshot row, absent from the
    # screen. A zero-coverage score would read as "scored badly" when the truth
    # is "not scored".
    newcomer = fresh_db.execute(
        """insert into security
           (name, mic, currency, country, primary_symbol, first_seen)
           values ('New', 'XNAS', 'USD', 'US', 'NEW', '2026-02-01') returning id"""
    ).fetchone()[0]

    report = run_scoring(fresh_db, as_of=AS_OF)

    assert report.skipped == 1
    assert (
        fresh_db.execute(
            "select count(*) from snapshot_daily where security_id = %s", (newcomer,)
        ).fetchone()[0]
        == 0
    )


def test_a_partial_write_leaves_all_three_tables_empty(fresh_db, universe, monkeypatch):
    # The whole run is one transaction (spec D9). Tomorrow's crossing diff
    # would read a half-scored day as a crossing for every security that never
    # got scored.
    def boom(*_args, **_kwargs):
        raise RuntimeError("injected")

    monkeypatch.setattr("screener.scoring.run.write_snapshots", boom)

    with pytest.raises(RuntimeError):
        run_scoring(fresh_db, as_of=AS_OF)

    counts = _counts(fresh_db)
    assert counts["metric_daily"] == 0
    assert counts["pillar_score_daily"] == 0
    assert counts["snapshot_daily"] == 0
    # The run row survives, still `running`, as the record that it died.
    assert fresh_db.execute("select outcome from scoring_run").fetchall() == [
        ("running",)
    ]


def test_a_second_live_run_for_the_same_date_is_rejected(fresh_db, universe):
    import psycopg

    run_scoring(fresh_db, as_of=AS_OF)

    with pytest.raises(psycopg.errors.ExclusionViolation):
        run_scoring(fresh_db, as_of=AS_OF)


def test_a_date_with_no_visible_bars_fails_before_writing_anything(fresh_db, universe):
    # An empty snapshot is worse than none: it is a day the diff would treat as
    # every security having stopped existing.
    with pytest.raises(NoBarsVisible):
        run_scoring(fresh_db, as_of=date(2026, 1, 2), cutoff_offset=-timedelta(days=400))

    assert _counts(fresh_db)["snapshot_daily"] == 0


def test_a_zero_momentum_weight_fails_loudly_rather_than_writing_an_empty_snapshot(
    fresh_db, universe
):
    # Pillar weights live in the database precisely so they can be tuned
    # without a redeploy -- so a weight version that zeroes momentum out is an
    # ordinary change, not a corrupt one, and it must not silently produce a
    # night with pillar rows but no snapshot rows (the same empty-snapshot
    # failure mode `NoBarsVisible` exists to prevent).
    fresh_db.execute(
        "update pillar_weight set weight = 0"
        " where weight_version_id = (select id from weight_version where code = 'v1')"
        "   and pillar_id = (select id from pillar where code = 'momentum')"
    )

    with pytest.raises(RuntimeError, match="momentum"):
        run_scoring(fresh_db, as_of=AS_OF)

    assert fresh_db.execute(
        "select count(*) from pillar_score_daily"
    ).fetchone()[0] == 0
    assert fresh_db.execute(
        "select count(*) from snapshot_daily"
    ).fetchone()[0] == 0


def test_reference_reads_what_the_migration_seeded(fresh_db):
    ref = reference(fresh_db)

    assert set(ref.metric_ids) == {"ret_3m", "ret_6m", "ret_12m", "off_52w_high"}
    assert ref.higher_is_better == {code: True for code in ref.metric_ids}
    assert ref.weights == {"momentum": Decimal(1)}
