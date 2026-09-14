"""What scoring writes, pinned before its functions learned to explain themselves.

Captured from the code as it stood before the explaining-form refactor (ui-swap
spec D12) and committed. That refactor must leave every row identical -- it
changes how a function reports an absence, never what is written. Regenerate
only for an intended change to what scoring computes, which also bumps the logic
version:

    CAPTURE_SCORING_GOLDEN=1 .venv/bin/python -m pytest tests/test_scoring_golden.py -n0

The fixture reaches a bank, a REIT, a thin bucket, a split inside its
market-cap window, a stale close, negative equity for debt/equity, and a
net-income fact in another currency. The second test checks it still does, so
the golden file cannot quietly become a record of a trivial night. The
remaining absence paths are pinned by the unedited `tests/test_scoring_ratios.py`
and `tests/test_scoring_metrics.py` instead of by this fixture.
"""

import json
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from screener.ingest import Fact, insert_facts, latest_values, metric_ids
from screener.scoring import run_scoring

GOLDEN = Path(__file__).resolve().parent / "golden" / "scoring_output.json"
AS_OF = date(2026, 3, 2)
SEEN = datetime(2026, 1, 1, tzinfo=timezone.utc)
# Inside price_daily's 2025 partition and read_bars' 13-month window.
OFFSETS = (380, 200, 100, 30, 0)
STALE_OFFSETS = (380, 200, 100, 30, 10)
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


def _facts(
    scale: str,
    *,
    net_income_currency: str = "USD",
    overrides: dict[str, str] | None = None,
) -> list[Fact]:
    """Four quarters of every flow and one balance sheet, scaled per company.

    Scaling spreads the companies' ratios apart so percentiles are not all ties;
    share counts are not scaled, so market cap moves with the close alone.
    """
    factor = Decimal(scale)
    overrides = overrides or {}
    out: list[Fact] = []
    for code, value in FLOWS.items():
        amount = Decimal(overrides.get(code, value)) * factor
        currency = net_income_currency if code == "net_income" else "USD"
        out += [Fact(code, end, "Q", amount, currency) for end in QUARTERS]
    for code, value in BALANCES.items():
        amount = Decimal(overrides.get(code, value))
        if code != "shares_outstanding":
            amount *= factor
        out.append(Fact(code, QUARTERS[0], "Q", amount, "USD"))
    return out


@pytest.fixture
def universe(fresh_db, an_observation):
    with fresh_db.cursor() as cur:
        cur.execute(
            "insert into sector_scheme (code, name) values ('yfinance', 'yfinance') returning id"
        )
        scheme = cur.fetchone()[0]
        cur.execute(
            "insert into peer_group (scheme_id, sector_node_id, level, code)"
            " values (%s, null, 0, 'market')",
            (scheme,),
        )

    industries: dict[str, int] = {}
    sectors: dict[str, int] = {}
    for sector_code, industry_code in (
        ("technology", "software"),
        ("financial-services", "banks-regional"),
        ("real-estate", "reit-retail"),
        ("real-estate", "real-estate-services"),
    ):
        with fresh_db.cursor() as cur:
            if sector_code not in sectors:
                cur.execute(
                    "insert into sector_node (scheme_id, level, code, name)"
                    " values (%s, 1, %s, %s) returning id",
                    (scheme, sector_code, sector_code),
                )
                sectors[sector_code] = cur.fetchone()[0]
                cur.execute(
                    "insert into peer_group (scheme_id, sector_node_id, level, code)"
                    " values (%s, %s, 1, %s)",
                    (scheme, sectors[sector_code], sector_code),
                )
            cur.execute(
                "insert into sector_node (scheme_id, parent_id, level, code, name)"
                " values (%s, %s, 2, %s, %s) returning id",
                (scheme, sectors[sector_code], industry_code, industry_code),
            )
            industries[industry_code] = cur.fetchone()[0]

    def company(symbol, industry, bump, facts, offsets=OFFSETS):
        security = fresh_db.execute(
            """insert into security
               (name, mic, currency, country, primary_symbol, first_seen)
               values (%s, 'XNAS', 'USD', 'US', %s, '2020-01-01') returning id""",
            (f"{symbol} Inc", symbol),
        ).fetchone()[0]
        fresh_db.execute(
            """insert into security_sector (security_id, sector_node_id, valid_from, source)
               values (%s, %s, '2020-01-01', 'yfinance')""",
            (security, industries[industry]),
        )
        observation = an_observation(security)
        for offset in offsets:
            close = Decimal(100 + bump) if offset == offsets[-1] else Decimal(100)
            fresh_db.execute(
                """insert into price_daily
                   (security_id, trade_date, open, high, low, close, volume,
                    observed_at, ingest_observation_id)
                   values (%s, %s, %s, %s, %s, %s, 1, %s, %s)""",
                (security, AS_OF - timedelta(days=offset), close, close, close, close,
                 SEEN, observation),
            )
        with fresh_db.cursor() as cur:
            insert_facts(
                cur, security, observation, SEEN, facts,
                latest_values(cur, security), metric_ids(cur),
            )
        return security, observation

    for i in range(20):
        company(f"S{i:02d}", "software", i, _facts(str(Decimal(1) + Decimal(i) / 10)))
    company("BANK", "banks-regional", 3, _facts("1.2"))
    company("REIT", "reit-retail", 5, _facts("0.8"))
    company("SVCS", "real-estate-services", 7, _facts("1.5"))
    split, observation = company("SPLT", "software", 9, _facts("1.1"))
    fresh_db.execute(
        """insert into corporate_action
           (security_id, effective_date, action_type, ratio, observed_at, ingest_observation_id)
           values (%s, %s, 'split', 2, %s, %s)""",
        (split, AS_OF - timedelta(days=3), SEEN, observation),
    )
    company("STAL", "software", 4, _facts("0.9"), offsets=STALE_OFFSETS)
    company("NEGQ", "software", 6, _facts("1", overrides={"stockholders_equity": "-100"}))
    company("FRGN", "software", 8, _facts("1", net_income_currency="EUR"))


def _output(conn) -> dict[str, list[list]]:
    """Every row scoring wrote, keyed by stable codes rather than generated ids."""
    return {
        "metric_daily": [list(row) for row in conn.execute(
            """select s.primary_symbol, m.code, md.raw_value::text, md.percentile::text,
                      pg.code, md.peer_count, md.fallback_level, md.period_basis,
                      md.period_end::text
                 from metric_daily md
                 join security s on s.id = md.security_id
                 join metric m on m.id = md.metric_id
                 join peer_group pg on pg.id = md.peer_group_id
             order by 1, 2"""
        ).fetchall()],
        "pillar_score_daily": [list(row) for row in conn.execute(
            """select s.primary_symbol, p.code, ps.score::text, ps.metric_count,
                      ps.coverage::text
                 from pillar_score_daily ps
                 join security s on s.id = ps.security_id
                 join pillar p on p.id = ps.pillar_id
             order by 1, 2"""
        ).fetchall()],
        "snapshot_daily": [list(row) for row in conn.execute(
            """select s.primary_symbol, sd.blended_score::text, sd.pillar_agreement,
                      sd.min_coverage::text, sd.worst_fallback_level
                 from snapshot_daily sd
                 join security s on s.id = sd.security_id
             order by 1"""
        ).fetchall()],
        "peer_group_stat": [list(row) for row in conn.execute(
            """select pg.code, m.code, st.member_count, st.deciles::text
                 from peer_group_stat st
                 join peer_group pg on pg.id = st.peer_group_id
                 join metric m on m.id = st.metric_id
             order by 1, 2"""
        ).fetchall()],
    }


def test_scoring_writes_exactly_what_it_wrote_before_the_explaining_form(fresh_db, universe):
    run_scoring(fresh_db, as_of=AS_OF)

    got = _output(fresh_db)

    if os.environ.get("CAPTURE_SCORING_GOLDEN"):
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n")
        pytest.skip(f"captured {GOLDEN}; review and commit it")

    assert got == json.loads(GOLDEN.read_text())


def test_the_golden_fixture_exercises_every_absence_path(fresh_db, universe):
    run_scoring(fresh_db, as_of=AS_OF)

    rows = _output(fresh_db)["metric_daily"]
    codes = {(symbol, code) for symbol, code, *_ in rows}
    market_ranked = {(symbol, code) for symbol, code, _, _, group, _, level, _, _ in rows if level == 0}
    valuation = {"earnings_yield", "ebitda_ev", "fcf_yield", "book_yield", "ffo_yield"}

    assert ("BANK", "book_yield") in codes and ("BANK", "roe") in codes
    assert ("REIT", "ffo_yield") in codes and ("REIT", "earnings_yield") not in codes
    assert ("SVCS", "earnings_yield") in market_ranked
    assert not {code for symbol, code in codes if symbol == "SPLT"} & valuation
    assert not {code for symbol, code in codes if symbol == "STAL"} & valuation
    assert ("NEGQ", "debt_to_equity") not in codes and ("NEGQ", "roic") in codes
    assert ("FRGN", "earnings_yield") not in codes and ("FRGN", "ebitda_ev") in codes
