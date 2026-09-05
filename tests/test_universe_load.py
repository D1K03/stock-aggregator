from datetime import date

import pytest

from screener.universe.load import (
    DepartureCeilingExceeded,
    apply,
    current_state,
    ensure_taxonomy,
)
from screener.universe.rows import UniverseRow

AS_OF = date(2026, 9, 5)
LATER = date(2026, 12, 5)


def row(symbol: str, *, cik: str = "", industry: str = "Consumer Electronics",
        sector: str = "Technology", name: str | None = None) -> UniverseRow:
    return UniverseRow(
        symbol=symbol, name=name or f"{symbol} Inc", index_name="sp500", mic="XNAS",
        currency="USD", cik=cik, yf_sector=sector, yf_industry=industry,
        gics_sector="Information Technology",
    )


def active_symbols(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("select primary_symbol from security where is_active")
        return {r[0] for r in cur.fetchall()}


def test_a_first_load_inserts_securities(fresh_db):
    apply(fresh_db, [row("AAPL"), row("MSFT")], as_of=AS_OF)
    assert active_symbols(fresh_db) == {"AAPL", "MSFT"}


def test_loading_the_same_csv_twice_changes_nothing(fresh_db):
    apply(fresh_db, [row("AAPL")], as_of=AS_OF)
    second = apply(fresh_db, [row("AAPL")], as_of=LATER)
    assert second.is_empty()
    assert second.unchanged == 1


# The departure tests below pass force=True. The ceiling is calibrated for a
# 1,500-name universe, where a real quarterly churn is under 2%; retiring one
# of two test securities is 50% and would trip a guard these tests are not
# about. `test_the_departure_ceiling_refuses_a_truncated_csv` is where the
# guard itself is exercised.
def test_a_departure_retires_rather_than_deletes(fresh_db):
    apply(fresh_db, [row("AAPL"), row("MSFT")], as_of=AS_OF)
    apply(fresh_db, [row("AAPL")], as_of=LATER, force=True)
    with fresh_db.cursor() as cur:
        cur.execute("select is_active, last_seen from security where primary_symbol = 'MSFT'")
        is_active, last_seen = cur.fetchone()
    assert is_active is False and last_seen == LATER


def test_a_departure_leaves_the_temporal_rows_open(fresh_db):
    """It left the universe, not existence: we do not know its symbol or sector changed."""
    apply(fresh_db, [row("MSFT")], as_of=AS_OF)
    apply(fresh_db, [], as_of=LATER, force=True)
    with fresh_db.cursor() as cur:
        cur.execute(
            "select ss.valid_to from security_symbol ss join security s on s.id = ss.security_id"
            " where s.primary_symbol = 'MSFT'"
        )
        assert cur.fetchone()[0] is None


def test_a_rename_keeps_the_same_security_and_closes_the_old_symbol(fresh_db):
    apply(fresh_db, [row("FB", cik="0001326801", name="Meta")], as_of=AS_OF)
    with fresh_db.cursor() as cur:
        cur.execute("select id from security where primary_symbol = 'FB'")
        before = cur.fetchone()[0]

    apply(fresh_db, [row("META", cik="0001326801", name="Meta")], as_of=LATER)

    with fresh_db.cursor() as cur:
        cur.execute("select id from security where primary_symbol = 'META'")
        assert cur.fetchone()[0] == before
        cur.execute(
            "select symbol, valid_from, valid_to from security_symbol"
            " where security_id = %s order by valid_from",
            (before,),
        )
        history = cur.fetchall()
    assert [h[0] for h in history] == ["FB", "META"]
    assert history[0][2] == LATER and history[1][2] is None


def test_a_reclassification_closes_the_old_row_adjacently(fresh_db):
    apply(fresh_db, [row("AAPL", industry="Consumer Electronics")], as_of=AS_OF)
    apply(fresh_db, [row("AAPL", industry="Software - Infrastructure")], as_of=LATER)
    with fresh_db.cursor() as cur:
        cur.execute(
            "select valid_from, valid_to from security_sector ss"
            " join security s on s.id = ss.security_id"
            " where s.primary_symbol = 'AAPL' order by valid_from"
        )
        rows = cur.fetchall()
    assert len(rows) == 2
    assert rows[0][1] == LATER == rows[1][0]
    assert rows[1][1] is None


def test_re_entry_reactivates_the_original_security(fresh_db):
    apply(fresh_db, [row("AAPL")], as_of=AS_OF)
    with fresh_db.cursor() as cur:
        cur.execute("select id from security where primary_symbol = 'AAPL'")
        original = cur.fetchone()[0]
    apply(fresh_db, [], as_of=LATER, force=True)
    apply(fresh_db, [row("AAPL")], as_of=date(2027, 3, 1))
    with fresh_db.cursor() as cur:
        cur.execute("select id, is_active, last_seen from security where primary_symbol = 'AAPL'")
        got = cur.fetchone()
    assert got == (original, True, None)


def test_the_departure_ceiling_refuses_a_truncated_csv(fresh_db):
    apply(fresh_db, [row(s) for s in ("A", "B", "C", "D", "E", "F", "G", "H", "I", "J")], as_of=AS_OF)
    with fresh_db.cursor() as cur:
        cur.execute("select count(*) from sector_node")
        nodes_before = cur.fetchone()[0]

    with pytest.raises(DepartureCeilingExceeded):
        apply(fresh_db, [row("A", industry="Something Entirely New")], as_of=LATER)

    assert len(active_symbols(fresh_db)) == 10
    with fresh_db.cursor() as cur:
        cur.execute("select count(*) from sector_node")
        # The refusal rolls back the taxonomy upsert too: a run that reports
        # doing nothing must not have left a new sector node behind.
        assert cur.fetchone()[0] == nodes_before


def test_force_overrides_the_ceiling(fresh_db):
    apply(fresh_db, [row(s) for s in ("A", "B", "C", "D", "E", "F", "G", "H", "I", "J")], as_of=AS_OF)
    apply(fresh_db, [row("A")], as_of=LATER, force=True)
    assert active_symbols(fresh_db) == {"A"}


def test_peer_groups_exist_for_market_and_every_sector(fresh_db):
    ensure_taxonomy(fresh_db, [row("AAPL"), row("XOM", sector="Energy", industry="Oil & Gas Integrated")])
    with fresh_db.cursor() as cur:
        cur.execute("select level, count(*) from peer_group group by level order by level")
        assert cur.fetchall() == [(0, 1), (1, 2)]


def test_industries_are_stored_as_level_two_nodes_under_their_sector(fresh_db):
    ensure_taxonomy(fresh_db, [row("AAPL")])
    with fresh_db.cursor() as cur:
        cur.execute(
            "select child.level, parent.code from sector_node child"
            " join sector_node parent on parent.id = child.parent_id"
            " where child.code = 'consumer-electronics'"
        )
        assert cur.fetchone() == (2, "technology")


def test_current_state_reports_what_the_reconciler_needs(fresh_db):
    apply(fresh_db, [row("AAPL", cik="0000320193")], as_of=AS_OF)
    state = current_state(fresh_db)
    assert len(state) == 1
    assert (state[0].symbol, state[0].cik, state[0].is_active) == ("AAPL", "0000320193", True)


def test_a_dry_run_writes_nothing_at_all(fresh_db, tmp_path, monkeypatch):
    """The spec says --dry-run touches nothing, and the taxonomy upsert is the
    easiest way to break that promise: on an autocommit connection it commits
    sector nodes before anyone has decided whether the load should happen."""
    from types import SimpleNamespace

    from screener.universe import load as load_module
    from screener.universe.rows import write_rows

    path = tmp_path / "universe.csv"
    write_rows(path, [row("AAPL")])
    monkeypatch.setattr(load_module, "settings", lambda: SimpleNamespace(database_url="unused"))
    monkeypatch.setattr(load_module.psycopg, "connect", lambda *a, **k: _NoClose(fresh_db))

    decided = load_module.load(path, as_of=AS_OF, dry_run=True)

    assert [r.symbol for r in decided.new] == ["AAPL"]
    with fresh_db.cursor() as cur:
        cur.execute("select count(*) from security")
        assert cur.fetchone()[0] == 0
        cur.execute("select count(*) from sector_node")
        assert cur.fetchone()[0] == 0


class _NoClose:
    """Hands `load` the test connection without letting its `with` block close it."""

    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *_exc):
        return False
