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
from screener.screen import RunChanged, RunRow, previous_run, read_screen, resolve_run, screen_params

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

    def split(
        self, security_id: int, effective_date: date, ratio: str, *, observed_at: datetime = SEEN
    ) -> None:
        if security_id not in self.observations:
            self.observations[security_id] = self.observe(security_id)
        self.conn.execute(
            """insert into corporate_action
               (security_id, effective_date, action_type, ratio, amount, currency,
                observed_at, ingest_observation_id)
               values (%s, %s, 'split', %s, null, 'USD', %s, %s)""",
            (security_id, effective_date, Decimal(ratio), observed_at,
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


# -- the page (D9, D11) ------------------------------------------------------


def ask(world: World, **query: object) -> dict[str, Any]:
    return read_screen(world.conn, screen_params({name: [str(value)] for name, value in query.items()}))


def symbols(payload: dict[str, Any]) -> list[str]:
    return [row["symbol"] for row in payload["rows"]]


def test_before_the_first_v2_night_the_screen_says_so(world):
    assert ask(world) == {"state": "awaiting_first_night"}


def test_delta_against_the_previous_night_and_new_for_a_newly_scored_security(world):
    old, new = world.security("OLD"), world.security("NEW")
    yesterday = AS_OF - timedelta(days=1)
    world.snapshot(world.run(yesterday), old, "70.0", as_of=yesterday)
    tonight = world.run()
    world.snapshot(tonight, old, "74.25")
    world.snapshot(tonight, new, "60")

    shown = ask(world)
    rows = {row["symbol"]: row for row in shown["rows"]}

    assert shown["previous_as_of"] == yesterday.isoformat()
    assert rows["OLD"]["delta"] == "4.3"
    assert rows["NEW"]["delta"] is None


def test_delta_is_null_across_a_weight_change(world):
    held = world.security("HELD")
    yesterday = AS_OF - timedelta(days=1)
    world.snapshot(world.run(yesterday, weight="v1"), held, "70", as_of=yesterday)
    world.snapshot(world.run(), held, "74")

    shown = ask(world)

    assert shown["previous_as_of"] is None
    assert shown["rows"][0]["delta"] is None


def test_a_pinned_night_serves_its_own_rows_after_a_newer_one_lands(world):
    held = world.security("HELD")
    first = world.run()
    world.snapshot(first, held, "50")
    tomorrow = AS_OF + timedelta(days=1)
    world.snapshot(world.run(tomorrow), held, "60", as_of=tomorrow)

    shown = ask(world, run=first)

    assert (shown["run"]["id"], shown["latest"]) == (first, False)
    assert shown["rows"][0]["score"] == "50.0"


def test_each_filter_narrows_the_rows_and_the_total(world):
    run = world.run()
    world.snapshot(run, world.security("TFUL"), "80", agreement=3)
    world.snapshot(run, world.security("TPRT"), "70", agreement=2, min_coverage="0.5")
    world.snapshot(run, world.security("OIL", "oil-gas-integrated"), "60")
    world.snapshot(run, world.security("LOOS", None), "50", agreement=0)

    def seen(**query: object) -> tuple[list[str], int]:
        shown = ask(world, **query)
        return symbols(shown), shown["total"]

    assert seen() == (["TFUL", "TPRT", "OIL", "LOOS"], 4)
    assert seen(sector="technology") == (["TFUL", "TPRT"], 2)
    assert seen(sector="unclassified") == (["LOOS"], 1)
    assert seen(sector="no-such-sector") == ([], 0)
    assert seen(agree=2) == (["TFUL", "TPRT"], 2)
    assert seen(partial="only") == (["TPRT"], 1)
    assert seen(partial="hide") == (["TFUL", "OIL", "LOOS"], 3)
    assert seen(sector="technology", partial="hide") == (["TFUL"], 1)
    assert ask(world, sector="energy")["sectors"] == [
        {"code": "energy", "name": "Energy"},
        {"code": "technology", "name": "Technology"},
        {"code": "unclassified", "name": "Unclassified"},
    ]


def test_each_sort_is_descending_with_nulls_last(world):
    yesterday = AS_OF - timedelta(days=1)
    before, run = world.run(yesterday), world.run()
    a, b, c = world.security("AAA"), world.security("BBB"), world.security("CCC")
    world.snapshot(before, a, "50", as_of=yesterday)
    world.snapshot(before, b, "10", as_of=yesterday)
    world.snapshot(run, a, "60", pillars={
        "valuation": ("20", "1"), "quality": ("90", "1"), "momentum": ("40", "1"),
    })
    world.snapshot(run, b, "55", pillars={"valuation": ("80", "1"), "momentum": ("70", "1")})
    world.snapshot(run, c, "70", pillars={"quality": ("10", "1"), "momentum": ("30", "1")})

    assert symbols(ask(world)) == ["CCC", "AAA", "BBB"]
    assert symbols(ask(world, sort="delta")) == ["BBB", "AAA", "CCC"]
    assert symbols(ask(world, sort="V")) == ["BBB", "AAA", "CCC"]
    assert symbols(ask(world, sort="Q")) == ["AAA", "CCC", "BBB"]
    assert symbols(ask(world, sort="M")) == ["BBB", "AAA", "CCC"]


def test_ties_break_on_security_id_so_pages_neither_overlap_nor_skip(world):
    run = world.run()
    ids = [world.security(f"T{i}") for i in range(5)]
    for security_id in reversed(ids):
        world.snapshot(run, security_id, "50")

    pages = [symbols(ask(world, offset=offset, limit=2)) for offset in (0, 2, 4)]

    assert pages == [["T0", "T1"], ["T2", "T3"], ["T4"]]
    assert ask(world, offset=10, limit=2)["total"] == 5


def test_the_sector_is_the_one_held_on_the_night_not_today(world):
    run = world.run()
    moved = world.security("MOVD", "oil-gas-integrated")
    reclassified = AS_OF + timedelta(days=1)
    world.conn.execute(
        "update security_sector set valid_to = %s where security_id = %s", (reclassified, moved)
    )
    world.conn.execute(
        """insert into security_sector (security_id, sector_node_id, valid_from, source)
           values (%s, %s, %s, 'yfinance')""",
        (moved, world.nodes["software"], reclassified),
    )
    world.snapshot(run, moved, "50")
    world.snapshot(run, world.security("UTIL", "utilities"), "40")

    shown = ask(world)

    assert [row["sector"] for row in shown["rows"]] == [
        {"code": "energy", "name": "Energy"},
        {"code": "utilities", "name": "Utilities"},
    ]


def test_the_tiles_count_the_whole_night_whatever_the_filters(world):
    run = world.run()
    full, part, loose = world.security("FULL"), world.security("PART"), world.security("LOOS", None)
    world.security("GONE", active=False)
    world.security("UNSC")
    world.snapshot(run, full, "80", agreement=3)
    world.snapshot(run, part, "60", agreement=2, min_coverage="0.5")
    world.snapshot(run, loose, "40")
    world.metric(run, full, "ret_3m", "0.1", level=1)
    # A thin bucket ranked against the market: counted.
    world.metric(run, part, "ret_3m", "0.1", level=0)
    # Unclassified, so the market is where it belongs rather than a fallback: not counted.
    world.metric(run, loose, "ret_3m", "0.1", level=0)

    assert ask(world, sector="technology", partial="hide")["tiles"] == {
        "scored": 3, "active_now": 4, "partial": 1, "agreement_3": 1, "market_ranked_values": 1,
    }


def test_closes_are_the_newest_thirty_including_a_bar_restamped_since_the_run(world):
    run = world.run()
    security_id = world.security("BARS")
    world.snapshot(run, security_id, "50")
    for back in range(35):
        world.bar(security_id, AS_OF - timedelta(days=back), str(100 + back))
    world.bar(security_id, AS_OF + timedelta(days=1), "1")
    # Ingest's settling window re-stamps the last week nightly (F13); the chart must
    # not lose its right edge for it.
    world.conn.execute(
        "update price_daily set observed_at = %s where security_id = %s and trade_date = %s",
        (datetime(2026, 9, 1, tzinfo=timezone.utc), security_id, AS_OF),
    )

    closes = ask(world)["rows"][0]["closes"]

    assert len(closes) == 30
    assert closes[0] == [(AS_OF - timedelta(days=29)).isoformat(), "129.00"]
    assert closes[-1] == [AS_OF.isoformat(), "100.00"]


def test_a_split_dated_after_the_run_still_applies_to_bars_it_already_restated(world):
    run = world.run()
    security_id = world.security("SPLT")
    world.snapshot(run, security_id, "50")
    # Older bars, fetched before the split, still at the pre-split close.
    for back in range(9, 4, -1):
        world.bar(
            security_id, AS_OF - timedelta(days=back), "100",
            observed_at=datetime(2026, 2, 20, tzinfo=timezone.utc),
        )
    # The newest bars, re-stamped by the settling window after the split's
    # effective date, already carry Yahoo's split-adjusted close.
    for back in range(4, -1, -1):
        world.bar(
            security_id, AS_OF - timedelta(days=back), "50",
            observed_at=datetime(2026, 3, 5, tzinfo=timezone.utc),
        )
    world.split(
        security_id, AS_OF + timedelta(days=2), "2",
        observed_at=datetime(2026, 3, 5, tzinfo=timezone.utc),
    )

    closes = ask(world)["rows"][0]["closes"]

    assert [close for _, close in closes] == ["50.00"] * len(closes)
