"""Migration 021: the input metrics, the flag, and the corrected index.

`metric.code` has to agree with the code parsing it, so the two ship together.
These tests are what makes "together" checkable.
"""

INPUT_CODES = {
    "revenue", "cost_of_revenue", "gross_profit", "research_and_development",
    "selling_general_admin", "operating_income", "ebit", "interest_expense",
    "pretax_income", "tax_provision", "net_income", "depreciation_amortisation",
    "operating_cash_flow", "capital_expenditure", "total_assets",
    "current_assets", "current_liabilities", "total_liabilities",
    "stockholders_equity", "cash_and_equivalents",
    "cash_and_short_term_investments", "current_debt", "long_term_debt",
    "total_debt", "net_ppe", "shares_basic_avg", "shares_diluted_avg",
    "shares_outstanding",
}


def test_twenty_eight_input_metrics_are_seeded(fresh_db):
    codes = {
        row[0]
        for row in fresh_db.execute(
            "select code from metric where is_input"
        ).fetchall()
    }
    assert codes == INPUT_CODES


def test_the_momentum_metrics_are_not_inputs(fresh_db):
    # The flag is what separates "stored as evidence" from "scored", and the
    # four price metrics are the other side of it.
    rows = fresh_db.execute(
        "select code from metric where not is_input order by code"
    ).fetchall()
    assert [r[0] for r in rows] == [
        "book_yield", "debt_to_equity", "earnings_yield", "ebitda_ev",
        "fcf_yield", "ffo_yield", "gross_margin", "interest_cover",
        "off_52w_high", "ret_12m", "ret_3m", "ret_6m", "roe", "roic"
    ]


def test_every_input_is_quarterly_cadence_and_a_known_unit(fresh_db):
    rows = fresh_db.execute(
        "select distinct cadence, unit from metric where is_input order by unit"
    ).fetchall()
    assert rows == [("quarterly", "currency"), ("quarterly", "shares")]


def test_the_three_share_counts_are_the_only_shares_unit(fresh_db):
    rows = fresh_db.execute(
        "select code from metric where unit = 'shares' order by code"
    ).fetchall()
    assert [r[0] for r in rows] == [
        "shares_basic_avg", "shares_diluted_avg", "shares_outstanding"
    ]


def test_no_derivation_is_seeded(fresh_db):
    # D3: a figure Yahoo computes from lines we already store is not stored.
    # These are the six that were measured as exactly reproducible.
    codes = {
        row[0] for row in fresh_db.execute("select code from metric").fetchall()
    }
    assert codes.isdisjoint(
        {"ebitda", "net_debt", "working_capital", "invested_capital",
         "tangible_book_value", "total_capitalization", "free_cash_flow"}
    )


def test_the_point_in_time_index_includes_period_type(fresh_db):
    # D6: without period_type a fiscal year collapses into its own Q4. And the
    # full order matters, not just that period_type is present somewhere: the
    # point-in-time read's `order by` is exactly
    # security_id, metric_id, period_end, period_type, observed_at desc, and
    # any transposition means Postgres can't satisfy its `distinct on` from
    # this index and sorts anyway -- paying the index's write cost on every
    # insert for nothing. Pin the whole column list, not a substring of it.
    definition = fresh_db.execute(
        "select indexdef from pg_indexes where indexname = %s",
        ("fundamental_fact_pit_idx2",),
    ).fetchone()
    assert definition is not None
    text = definition[0]
    assert (
        "(security_id, metric_id, period_end, period_type, observed_at DESC)"
        in text
    )


def test_the_old_index_that_omitted_period_type_is_gone(fresh_db):
    row = fresh_db.execute(
        "select indexname from pg_indexes where indexname = %s",
        ("fundamental_fact_pit_idx",),
    ).fetchone()
    assert row is None
