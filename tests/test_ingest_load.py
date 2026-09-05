from datetime import date
from decimal import Decimal

import pytest

from screener.ingest.load import (
    insert_actions,
    insert_settled_bars,
    record_observation,
    upsert_unsettled_bars,
)
from screener.ingest.parse import Action, Bar
from screener.ingest.window import settling_cutoff

TODAY = date(2026, 9, 5)
CUTOFF = settling_cutoff(TODAY)


def bar(day, close="100", volume=10):
    return Bar(day, Decimal(close), Decimal(close), Decimal(close), Decimal(close), volume)


def test_a_settled_bar_that_already_exists_is_not_modified(fresh_db, ingest_ctx):
    sid, obs = ingest_ctx
    old = date(2026, 1, 5)
    insert_settled_bars(fresh_db.cursor(), sid, obs, [bar(old, "100")], CUTOFF)
    insert_settled_bars(fresh_db.cursor(), sid, obs, [bar(old, "999")], CUTOFF)
    got = fresh_db.execute(
        "select close from price_daily where security_id=%s and trade_date=%s", (sid, old)
    ).fetchone()[0]
    assert got == Decimal("100")


def test_an_unsettled_bar_is_upserted_and_the_change_is_reported(fresh_db, ingest_ctx):
    sid, obs = ingest_ctx
    recent = date(2026, 9, 4)
    upsert_unsettled_bars(fresh_db.cursor(), sid, obs, [bar(recent, "100", 10)], CUTOFF)
    changes = upsert_unsettled_bars(
        fresh_db.cursor(), sid, obs, [bar(recent, "100", 99)], CUTOFF
    )
    assert [(c.field, c.old, c.new) for c in changes] == [("volume", 10, 99)]
    got = fresh_db.execute(
        "select volume from price_daily where security_id=%s and trade_date=%s", (sid, recent)
    ).fetchone()[0]
    assert got == 99


def test_an_unchanged_unsettled_bar_reports_no_change(fresh_db, ingest_ctx):
    sid, obs = ingest_ctx
    recent = date(2026, 9, 4)
    upsert_unsettled_bars(fresh_db.cursor(), sid, obs, [bar(recent)], CUTOFF)
    assert upsert_unsettled_bars(fresh_db.cursor(), sid, obs, [bar(recent)], CUTOFF) == []


def test_the_two_paths_split_on_the_cutoff(fresh_db, ingest_ctx):
    sid, obs = ingest_ctx
    bars = [bar(date(2026, 1, 5)), bar(date(2026, 9, 4))]
    assert insert_settled_bars(fresh_db.cursor(), sid, obs, bars, CUTOFF) == 1
    assert len(upsert_unsettled_bars(fresh_db.cursor(), sid, obs, bars, CUTOFF)) == 0
    count = fresh_db.execute(
        "select count(*) from price_daily where security_id=%s", (sid,)
    ).fetchone()[0]
    assert count == 2


def test_a_corporate_action_already_held_is_not_duplicated(fresh_db, ingest_ctx):
    sid, obs = ingest_ctx
    action = Action(date(2026, 9, 3), "split", Decimal("2"), None)
    insert_actions(fresh_db.cursor(), sid, obs, [action])
    insert_actions(fresh_db.cursor(), sid, obs, [action])
    count = fresh_db.execute(
        "select count(*) from corporate_action where security_id=%s", (sid,)
    ).fetchone()[0]
    assert count == 1


def test_a_differing_corporate_action_is_reported_and_not_written(fresh_db, ingest_ctx):
    sid, obs = ingest_ctx
    insert_actions(
        fresh_db.cursor(), sid, obs, [Action(date(2026, 9, 3), "dividend", None, Decimal("0.24"))]
    )
    changes = insert_actions(
        fresh_db.cursor(), sid, obs, [Action(date(2026, 9, 3), "dividend", None, Decimal("0.25"))]
    )
    assert [(c.field, c.old, c.new) for c in changes] == [
        ("amount", Decimal("0.24"), Decimal("0.25"))
    ]
    held = fresh_db.execute(
        "select amount from corporate_action where security_id=%s", (sid,)
    ).fetchone()[0]
    assert held == Decimal("0.24")


def test_a_split_and_a_dividend_on_one_day_are_both_kept(fresh_db, ingest_ctx):
    sid, obs = ingest_ctx
    day = date(2026, 9, 3)
    insert_actions(
        fresh_db.cursor(),
        sid,
        obs,
        [
            Action(day, "split", Decimal("2"), None),
            Action(day, "dividend", None, Decimal("0.24")),
        ],
    )
    count = fresh_db.execute(
        "select count(*) from corporate_action where security_id=%s", (sid,)
    ).fetchone()[0]
    assert count == 2
