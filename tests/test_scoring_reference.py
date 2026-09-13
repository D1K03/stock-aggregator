"""Migration 019 seeds what the scoring code looks up by name.

`metric.code` has to agree with the code computing it, so the two ship
together (spec D11). These tests are what makes "together" checkable.
"""

from decimal import Decimal

import psycopg
import pytest


def test_five_pillars_are_seeded_and_event_risk_is_not_one(fresh_db):
    codes = [
        row[0]
        for row in fresh_db.execute("select code from pillar order by code").fetchall()
    ]
    assert codes == ["insider", "momentum", "quality", "sentiment", "valuation"]


def test_the_four_momentum_metrics_are_seeded(fresh_db):
    rows = fresh_db.execute(
        """select m.code, m.unit, m.higher_is_better, m.cadence, p.code
             from metric m join pillar p on p.id = m.pillar_id
            where not m.is_input and p.code = 'momentum'
         order by m.code"""
    ).fetchall()
    assert rows == [
        ("off_52w_high", "ratio", True, "daily", "momentum"),
        ("ret_12m", "ratio", True, "daily", "momentum"),
        ("ret_3m", "ratio", True, "daily", "momentum"),
        ("ret_6m", "ratio", True, "daily", "momentum"),
    ]


def test_the_ten_ratios_are_seeded_with_their_pillar_direction_and_cadence(fresh_db):
    rows = fresh_db.execute(
        """select m.code, m.unit, m.higher_is_better, m.cadence, p.code, m.is_input
             from metric m join pillar p on p.id = m.pillar_id
            where p.code in ('valuation', 'quality') and not m.is_input
         order by p.code, m.code"""
    ).fetchall()
    assert rows == [
        ("debt_to_equity", "ratio", False, "quarterly", "quality", False),
        ("gross_margin", "ratio", True, "quarterly", "quality", False),
        ("interest_cover", "ratio", True, "quarterly", "quality", False),
        ("roe", "ratio", True, "quarterly", "quality", False),
        ("roic", "ratio", True, "quarterly", "quality", False),
        ("book_yield", "ratio", True, "daily", "valuation", False),
        ("earnings_yield", "ratio", True, "daily", "valuation", False),
        ("ebitda_ev", "ratio", True, "daily", "valuation", False),
        ("fcf_yield", "ratio", True, "daily", "valuation", False),
        ("ffo_yield", "ratio", True, "daily", "valuation", False),
    ]


def test_weight_version_v1_puts_all_weight_on_momentum(fresh_db):
    rows = fresh_db.execute(
        """select p.code, w.weight
             from pillar_weight w
             join pillar p on p.id = w.pillar_id
             join weight_version v on v.id = w.weight_version_id
            where v.code = 'v1'"""
    ).fetchall()
    assert rows == [("momentum", Decimal("1.0"))]


def test_weight_version_v2_weights_the_three_computed_pillars_equally(fresh_db):
    rows = fresh_db.execute(
        """select p.code, w.weight
             from pillar_weight w
             join pillar p on p.id = w.pillar_id
             join weight_version v on v.id = w.weight_version_id
            where v.code = 'v2'
         order by p.code"""
    ).fetchall()
    assert rows == [
        ("momentum", 1),
        ("quality", 1),
        ("valuation", 1),
    ]


def test_both_logic_versions_are_seeded_with_the_descriptions_the_code_selects_on(fresh_db):
    rows = fresh_db.execute(
        "select description from scoring_logic_version order by id"
    ).fetchall()
    assert rows == [
        ("v1 momentum: four price metrics, sector percentiles",),
        ("v2 momentum, valuation, quality: sector percentiles, industry-applicable ratios",),
    ]


def test_period_basis_accepts_ttm_annual_and_null_and_nothing_else(fresh_db):
    columns = fresh_db.execute(
        """select is_nullable, data_type from information_schema.columns
            where table_name = 'metric_daily' and column_name = 'period_basis'"""
    ).fetchall()
    assert columns == [("YES", "text")]

    constraint = fresh_db.execute(
        """select pg_get_constraintdef(c.oid)
             from pg_constraint c join pg_class t on t.oid = c.conrelid
            where t.relname = 'metric_daily' and c.contype = 'c'
              and pg_get_constraintdef(c.oid) like '%%period_basis%%'"""
    ).fetchall()
    assert len(constraint) == 1
    assert "'TTM'" in constraint[0][0] and "'A'" in constraint[0][0]
