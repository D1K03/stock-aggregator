"""The screen's reads against a real database (ui-swap spec D7-D9, D11).

Runs, snapshots and pillar scores are written by hand rather than by running
scoring: what is under test is which night is served and how its rows are
filtered, sorted and paged, and that wants scores chosen for the purpose.
`test_screen_security.py` is where scoring really runs.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from screener.scoring import LOGIC_DESCRIPTION
from screener.screen import RunChanged, RunRow, previous_run, resolve_run

AS_OF = date(2026, 3, 2)
SEEN = datetime(2026, 3, 2, 22, tzinfo=timezone.utc)
V1_LOGIC = "v1 momentum: four price metrics, sector percentiles"


@dataclass
class World:
    # `Any`, because the fixture's connection is untyped and a typed one would make
    # every `fetchone()[0]` below an optional-subscript error.
    conn: Any
    observe: Callable[[int], int]
    market: int
    groups: dict[str, int]
    nodes: dict[str, int]
    observations: dict[int, int] = field(default_factory=dict)

    def security(
        self,
        symbol: str,
        node: str | None = "software",
        *,
        active: bool = True,
        classified_from: date = date(2020, 1, 1),
    ) -> int:
        security_id = self.conn.execute(
            """insert into security
               (name, mic, currency, country, primary_symbol, first_seen, is_active)
               values (%s, 'XNAS', 'USD', 'US', %s, '2020-01-01', %s) returning id""",
            (f"{symbol} Inc", symbol, active),
        ).fetchone()[0]
        self.conn.execute(
            """insert into security_symbol (security_id, symbol, mic, valid_from, source)
               values (%s, %s, 'XNAS', '2020-01-01', 'test')""",
            (security_id, symbol),
        )
        if node is not None:
            self.conn.execute(
                """insert into security_sector (security_id, sector_node_id, valid_from, source)
                   values (%s, %s, %s, 'yfinance')""",
                (security_id, self.nodes[node], classified_from),
            )
        return security_id

    def run(
        self,
        as_of: date = AS_OF,
        *,
        logic: str = LOGIC_DESCRIPTION,
        weight: str = "v2",
        outcome: str = "ok",
        status: str = "live",
    ) -> int:
        started = datetime.combine(as_of, time(23), tzinfo=timezone.utc)
        return self.conn.execute(
            """insert into scoring_run
               (as_of_range, cutoff_offset, logic_version_id, weight_version_id, status,
                emits_alerts, git_sha, config_hash, started_at, finished_at, outcome)
               select daterange(%(as_of)s, %(next)s, '[)'), interval '30 hours', l.id, w.id,
                      %(status)s, false, 'abc1234', %(hash)s, %(started)s, %(started)s,
                      %(outcome)s
                 from scoring_logic_version l, weight_version w
                where l.description = %(logic)s and w.code = %(weight)s
               returning id""",
            {
                "as_of": as_of, "next": as_of + timedelta(days=1), "status": status,
                "hash": b"\x9f\x3a", "started": started, "outcome": outcome,
                "logic": logic, "weight": weight,
            },
        ).fetchone()[0]

    def snapshot(
        self,
        run_id: int,
        security_id: int,
        score: str,
        *,
        as_of: date = AS_OF,
        agreement: int = 1,
        min_coverage: str = "1",
        pillars: dict[str, tuple[str, str]] | None = None,
    ) -> None:
        """A snapshot and its pillar rows; by default one Momentum pillar at the blended score."""
        self.conn.execute(
            """insert into snapshot_daily
               (as_of, scoring_run_id, security_id, blended_score, pillar_agreement,
                min_coverage, worst_fallback_level)
               values (%s, %s, %s, %s, %s, %s, 1)""",
            (as_of, run_id, security_id, Decimal(score), agreement, Decimal(min_coverage)),
        )
        for code, (pillar_score, coverage) in (pillars or {"momentum": (score, "1")}).items():
            self.conn.execute(
                """insert into pillar_score_daily
                   (as_of, scoring_run_id, security_id, pillar_id, score, metric_count, coverage)
                   select %s, %s, %s, p.id, %s, 1, %s from pillar p where p.code = %s""",
                (as_of, run_id, security_id, Decimal(pillar_score), Decimal(coverage), code),
            )

    def metric(
        self, run_id: int, security_id: int, code: str, raw: str, *, level: int = 1
    ) -> None:
        group = self.market if level == 0 else self.groups["technology"]
        self.conn.execute(
            """insert into metric_daily
               (as_of, scoring_run_id, security_id, metric_id, raw_value, percentile,
                peer_group_id, peer_count, fallback_level)
               select %s, %s, %s, m.id, %s, 50, %s, 20, %s from metric m where m.code = %s""",
            (AS_OF, run_id, security_id, Decimal(raw), group, level, code),
        )

    def bar(
        self, security_id: int, day: date, close: str, *, observed_at: datetime = SEEN
    ) -> None:
        if security_id not in self.observations:
            self.observations[security_id] = self.observe(security_id)
        value = Decimal(close)
        self.conn.execute(
            """insert into price_daily
               (security_id, trade_date, open, high, low, close, volume, observed_at,
                ingest_observation_id)
               values (%s, %s, %s, %s, %s, %s, 1, %s, %s)""",
            (security_id, day, value, value, value, value, observed_at,
             self.observations[security_id]),
        )


@pytest.fixture
def world(fresh_db, an_observation) -> World:
    """Three sectors: Technology and Energy each with one industry, and Utilities
    with none, so a security can be classified straight at a sector node."""
    scheme = fresh_db.execute(
        "insert into sector_scheme (code, name) values ('yfinance', 'yfinance') returning id"
    ).fetchone()[0]
    market = fresh_db.execute(
        "insert into peer_group (scheme_id, sector_node_id, level, code)"
        " values (%s, null, 0, 'market') returning id",
        (scheme,),
    ).fetchone()[0]
    groups: dict[str, int] = {}
    nodes: dict[str, int] = {}
    for sector_code, sector_name, industry_code, industry_name in (
        ("technology", "Technology", "software", "Software"),
        ("energy", "Energy", "oil-gas-integrated", "Oil & Gas Integrated"),
        ("utilities", "Utilities", None, None),
    ):
        nodes[sector_code] = fresh_db.execute(
            "insert into sector_node (scheme_id, level, code, name)"
            " values (%s, 1, %s, %s) returning id",
            (scheme, sector_code, sector_name),
        ).fetchone()[0]
        groups[sector_code] = fresh_db.execute(
            "insert into peer_group (scheme_id, sector_node_id, level, code)"
            " values (%s, %s, 1, %s) returning id",
            (scheme, nodes[sector_code], sector_code),
        ).fetchone()[0]
        if industry_code is not None:
            nodes[industry_code] = fresh_db.execute(
                "insert into sector_node (scheme_id, parent_id, level, code, name)"
                " values (%s, %s, 2, %s, %s) returning id",
                (scheme, nodes[sector_code], industry_code, industry_name),
            ).fetchone()[0]
    return World(fresh_db, an_observation, market, groups, nodes)


def _serve(world: World, pinned: int | None = None) -> tuple[RunRow, bool]:
    resolved = resolve_run(world.conn, pinned)
    assert resolved is not None
    return resolved


# -- which night (D7, D8) ----------------------------------------------------


def test_no_night_scored_under_v2_means_there_is_nothing_to_serve(world):
    world.run(logic=V1_LOGIC, weight="v1")

    assert resolve_run(world.conn, None) is None


def test_a_failed_night_is_passed_over_for_the_last_good_one(world):
    good = world.run(AS_OF)
    world.run(AS_OF + timedelta(days=1), outcome="failed")

    run, latest = _serve(world)

    assert (run.id, run.as_of, latest) == (good, AS_OF, True)


def test_the_latest_good_live_night_is_served_with_its_provenance(world):
    world.run(AS_OF)
    newer = world.run(AS_OF + timedelta(days=1))
    world.run(AS_OF + timedelta(days=2), status="experiment")

    run, latest = _serve(world)

    assert run.id == newer and latest
    assert (run.weight_version, run.cutoff_offset_seconds, run.git_sha, run.config_hash) == (
        "v2", 108000, "abc1234", "9f3a",
    )


def test_a_pinned_night_is_still_served_after_a_newer_one_lands(world):
    pinned = world.run(AS_OF)
    world.run(AS_OF + timedelta(days=1))

    run, latest = _serve(world, pinned)

    assert run.id == pinned and latest is False


@pytest.mark.parametrize("which", ["unknown", "v1", "failed"])
def test_a_pinned_night_that_does_not_qualify_is_refused(world, which):
    world.run(AS_OF)
    if which == "unknown":
        pinned = 999_999
    elif which == "v1":
        pinned = world.run(AS_OF - timedelta(days=1), logic=V1_LOGIC, weight="v1")
    else:
        pinned = world.run(AS_OF + timedelta(days=1), outcome="failed")

    with pytest.raises(RunChanged) as caught:
        resolve_run(world.conn, pinned)

    assert caught.value.run_id == pinned


def test_delta_compares_with_the_last_good_earlier_night_under_the_same_weights(world):
    world.run(AS_OF - timedelta(days=3))
    wanted = world.run(AS_OF - timedelta(days=2))
    world.run(AS_OF - timedelta(days=1), outcome="failed")
    run, _ = _serve(world, world.run(AS_OF))

    previous = previous_run(world.conn, run)

    assert previous is not None
    assert (previous.id, previous.as_of) == (wanted, AS_OF - timedelta(days=2))


def test_a_change_of_weights_starts_delta_afresh(world):
    world.run(AS_OF - timedelta(days=1), weight="v1")
    run, _ = _serve(world, world.run(AS_OF))

    assert previous_run(world.conn, run) is None
