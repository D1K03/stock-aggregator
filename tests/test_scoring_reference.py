"""Migration 019 seeds what the scoring code looks up by name.

`metric.code` has to agree with the code computing it, so the two ship
together (spec D11). These tests are what makes "together" checkable.
"""

from decimal import Decimal


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
         order by m.code"""
    ).fetchall()
    assert rows == [
        ("off_52w_high", "ratio", True, "daily", "momentum"),
        ("ret_12m", "ratio", True, "daily", "momentum"),
        ("ret_3m", "ratio", True, "daily", "momentum"),
        ("ret_6m", "ratio", True, "daily", "momentum"),
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


def test_one_logic_version_is_seeded_with_the_description_the_code_selects_on(fresh_db):
    rows = fresh_db.execute(
        "select description from scoring_logic_version"
    ).fetchall()
    assert rows == [("v1 momentum: four price metrics, sector percentiles",)]
