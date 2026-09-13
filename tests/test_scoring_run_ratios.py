"""Three pillars in one night, against a real database.

`test_scoring_run.py` covers the run's lifecycle, on securities with no
fundamentals. This file covers what fundamentals add: pillars measured against
the metrics that apply, coverage that cannot overstate itself, a thin bucket
ranked against the market, and the period each ratio was computed on.

Every company has the same fundamentals -- the ones `test_scoring_ratios.py`
checks by hand -- and differs only in its last close, so market cap and every
yield differ while every quality ratio ties.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from screener.ingest import Fact, insert_facts, latest_values, metric_ids
from screener.scoring import MIN_PEERS, run_scoring

AS_OF = date(2026, 3, 2)
SEEN = datetime(2026, 1, 1, tzinfo=timezone.utc)
# See `test_scoring_run.py`: inside `price_daily`'s 2025 partition and the bar window.
OFFSETS = (380, 200, 100, 30, 0)
QUARTERS = (date(2025, 12, 31), date(2025, 9, 30), date(2025, 6, 30), date(2025, 3, 31))
FLOWS = {
    "net_income": "25",
    "revenue": "250",
    "gross_profit": "100",
    "ebit": "40",
    "depreciation_amortisation": "10",
    "operating_cash_flow": "50",
    "capital_expenditure": "-20",
    "interest_expense": "5",
    "tax_provision": "8",
    "pretax_income": "32",
}
BALANCES = {
    "stockholders_equity": "800",
    "total_debt": "400",
    "cash_and_equivalents": "200",
    "shares_outstanding": "100",
}


def _facts() -> list[Fact]:
    out = [
        Fact(code, end, "Q", Decimal(value), "USD")
        for code, value in FLOWS.items()
        for end in QUARTERS
    ]
    out += [Fact(code, QUARTERS[0], "Q", Decimal(value), "USD") for code, value in BALANCES.items()]
    return out


@pytest.fixture
def market(fresh_db, an_observation):
    """Twenty software companies with fundamentals and one without, one regional
    bank, and one real-estate services firm -- a sector of one."""
    with fresh_db.cursor() as cur:
        cur.execute(
            "insert into sector_scheme (code, name) values ('yfinance', 'yfinance') returning id"
        )
        scheme = cur.fetchone()[0]
        cur.execute(
            "insert into peer_group (scheme_id, sector_node_id, level, code)"
            " values (%s, null, 0, 'market') returning id",
            (scheme,),
        )
        market_group = cur.fetchone()[0]

    industries: dict[str, int] = {}
    for sector_code, industry_code in (
        ("technology", "software"),
        ("financial-services", "banks-regional"),
        ("real-estate", "real-estate-services"),
    ):
        with fresh_db.cursor() as cur:
            cur.execute(
                "insert into sector_node (scheme_id, level, code, name)"
                " values (%s, 1, %s, %s) returning id",
                (scheme, sector_code, sector_code),
            )
            sector = cur.fetchone()[0]
            cur.execute(
                "insert into sector_node (scheme_id, parent_id, level, code, name)"
                " values (%s, %s, 2, %s, %s) returning id",
                (scheme, sector, industry_code, industry_code),
            )
            industries[industry_code] = cur.fetchone()[0]
            cur.execute(
                "insert into peer_group (scheme_id, sector_node_id, level, code)"
                " values (%s, %s, 1, %s)",
                (scheme, sector, sector_code),
            )

    def company(i: int, industry_code: str, *, facts: bool = True) -> int:
        security = fresh_db.execute(
            """insert into security
               (name, mic, currency, country, primary_symbol, first_seen)
               values (%s, 'XNAS', 'USD', 'US', %s, '2020-01-01') returning id""",
            (f"Co {i}", f"S{i:03d}"),
        ).fetchone()[0]
        fresh_db.execute(
            """insert into security_sector (security_id, sector_node_id, valid_from, source)
               values (%s, %s, '2020-01-01', 'yfinance')""",
            (security, industries[industry_code]),
        )
        observation = an_observation(security)
        for offset in OFFSETS:
            close = Decimal(100 + i) if offset == 0 else Decimal(100)
            fresh_db.execute(
                """insert into price_daily
                   (security_id, trade_date, open, high, low, close, volume,
                    observed_at, ingest_observation_id)
                   values (%s, %s, %s, %s, %s, %s, 1, %s, %s)""",
                (security, AS_OF - timedelta(days=offset), close, close, close, close, SEEN, observation),
            )
        if facts:
            with fresh_db.cursor() as cur:
                insert_facts(
                    cur, security, observation, SEEN, _facts(),
                    latest_values(cur, security), metric_ids(cur),
                )
        return security

    software = [company(i, "software") for i in range(MIN_PEERS)]
    return {
        "market": market_group,
        "software": software,
        "bare": company(MIN_PEERS, "software", facts=False),
        "bank": company(MIN_PEERS + 1, "banks-regional"),
        "realty": company(MIN_PEERS + 2, "real-estate-services"),
    }


def _pillars(conn, security_id):
    return {
        code: (score, count, coverage)
        for code, score, count, coverage in conn.execute(
            """select p.code, ps.score, ps.metric_count, ps.coverage
                 from pillar_score_daily ps join pillar p on p.id = ps.pillar_id
                where ps.security_id = %s""",
            (security_id,),
        ).fetchall()
    }


def _metric(conn, security_id, code):
    return conn.execute(
        """select md.peer_group_id, md.fallback_level, md.peer_count,
                  md.period_basis, md.period_end
             from metric_daily md join metric m on m.id = md.metric_id
            where md.security_id = %s and m.code = %s""",
        (security_id, code),
    ).fetchone()


def test_a_company_with_fundamentals_is_scored_on_three_pillars(fresh_db, market):
    run_scoring(fresh_db, as_of=AS_OF)

    got = _pillars(fresh_db, market["software"][0])

    assert set(got) == {"momentum", "valuation", "quality"}
    assert got["valuation"][1:] == (3, Decimal(1))
    assert got["quality"][1:] == (4, Decimal(1))


def test_a_banks_coverage_counts_only_the_metrics_that_apply_to_a_bank(fresh_db, market):
    run_scoring(fresh_db, as_of=AS_OF)

    got = _pillars(fresh_db, market["bank"])

    # ROE alone is full Quality coverage for a bank, not one of four (D10).
    assert got["quality"][1:] == (1, Decimal(1))
    assert got["valuation"][1:] == (2, Decimal(1))


def test_a_security_without_fundamentals_is_momentum_only_and_says_so(fresh_db, market):
    run_scoring(fresh_db, as_of=AS_OF)

    bare = market["bare"]
    blended, min_coverage = fresh_db.execute(
        "select blended_score, min_coverage from snapshot_daily where security_id = %s",
        (bare,),
    ).fetchone()
    pillars = _pillars(fresh_db, bare)

    assert set(pillars) == {"momentum"}
    assert blended == pillars["momentum"][0]
    assert min_coverage == 0


def test_a_thin_bucket_is_ranked_against_every_producer_in_the_market(fresh_db, market):
    run_scoring(fresh_db, as_of=AS_OF)

    realty = _metric(fresh_db, market["realty"], "earnings_yield")
    software = _metric(fresh_db, market["software"][0], "earnings_yield")

    # 20 software companies, the bank and the realty firm produced earnings_yield.
    assert realty[:3] == (market["market"], 0, 22)
    assert software[1:3] == (1, 20)


def test_ratios_record_their_period_and_momentum_does_not(fresh_db, market):
    run_scoring(fresh_db, as_of=AS_OF)

    sid = market["software"][0]

    assert _metric(fresh_db, sid, "earnings_yield")[3:] == ("TTM", date(2025, 12, 31))
    assert _metric(fresh_db, sid, "debt_to_equity")[3:] == (None, date(2025, 12, 31))
    assert _metric(fresh_db, sid, "ret_12m")[3:] == (None, None)


def test_equal_weights_blend_to_the_mean_of_the_three_pillars(fresh_db, market):
    run_scoring(fresh_db, as_of=AS_OF)

    sid = market["software"][5]
    scores = [score for score, _, _ in _pillars(fresh_db, sid).values()]
    blended = fresh_db.execute(
        "select blended_score from snapshot_daily where security_id = %s", (sid,)
    ).fetchone()[0]

    assert len(scores) == 3
    assert round(blended, 10) == round(sum(scores) / 3, 10)


def test_a_split_this_week_leaves_valuation_absent_and_quality_standing(fresh_db, market, an_observation):
    sid = market["software"][1]
    fresh_db.execute(
        """insert into corporate_action
           (security_id, effective_date, action_type, ratio, observed_at, ingest_observation_id)
           values (%s, %s, 'split', 2, %s, %s)""",
        (sid, AS_OF - timedelta(days=3), SEEN, an_observation(sid)),
    )

    run_scoring(fresh_db, as_of=AS_OF)

    got = _pillars(fresh_db, sid)
    assert "valuation" not in got
    assert got["quality"][1] == 4


def test_the_night_logs_how_each_ratio_was_assembled(fresh_db, market, caplog):
    with caplog.at_level(logging.INFO, logger="screener.scoring.run"):
        run_scoring(fresh_db, as_of=AS_OF)

    # 23 securities it applies to; the bare software company has nothing.
    assert "earnings_yield: 22 TTM, 0 annual, 0 at a date, 1 absent" in caplog.text
    assert "debt_to_equity: 0 TTM, 0 annual, 21 at a date, 1 absent" in caplog.text
