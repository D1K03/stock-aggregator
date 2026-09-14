"""One security against a really scored night (ui-swap spec D10, D13, D14).

Scoring runs for real here. A reproduction that drifted from `score()` fails
`test_every_stored_metric_reproduces`, before anyone sees a panel full of
mismatches (plan amendment P5).
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from screener.ingest import Fact, insert_facts, latest_values, metric_ids
from screener.scoring import Absent, resolve, run_scoring
from screener.screen import Reproduction, reproduce, resolve_run

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


def _facts(scale: str, currency: str = "USD") -> list[Fact]:
    """Four quarters of every flow and one balance sheet, scaled per company; share
    counts unscaled, so market cap moves with the close alone."""
    factor = Decimal(scale)
    out = [
        Fact(code, end, "Q", Decimal(value) * factor, currency)
        for code, value in FLOWS.items()
        for end in QUARTERS
    ]
    out += [
        Fact(code, QUARTERS[0], "Q",
             Decimal(value) * (Decimal(1) if code == "shares_outstanding" else factor), currency)
        for code, value in BALANCES.items()
    ]
    return out


@pytest.fixture
def scored(fresh_db, an_observation) -> dict[str, int]:
    """Twenty software companies, a regional bank, a company reporting in euros while
    it trades in dollars, and one with neither bars nor facts, scored for AS_OF."""
    scheme = fresh_db.execute(
        "insert into sector_scheme (code, name) values ('yfinance', 'yfinance') returning id"
    ).fetchone()[0]
    fresh_db.execute(
        "insert into peer_group (scheme_id, sector_node_id, level, code)"
        " values (%s, null, 0, 'market')",
        (scheme,),
    )
    industries: dict[str, int] = {}
    for sector_code, sector_name, industry_code, industry_name in (
        ("technology", "Technology", "software", "Software"),
        ("financial-services", "Financial Services", "banks-regional", "Banks - Regional"),
    ):
        sector = fresh_db.execute(
            "insert into sector_node (scheme_id, level, code, name)"
            " values (%s, 1, %s, %s) returning id",
            (scheme, sector_code, sector_name),
        ).fetchone()[0]
        fresh_db.execute(
            "insert into peer_group (scheme_id, sector_node_id, level, code)"
            " values (%s, %s, 1, %s)",
            (scheme, sector, sector_code),
        )
        industries[industry_code] = fresh_db.execute(
            "insert into sector_node (scheme_id, parent_id, level, code, name)"
            " values (%s, %s, 2, %s, %s) returning id",
            (scheme, sector, industry_code, industry_name),
        ).fetchone()[0]

    ids: dict[str, int] = {}

    def company(symbol: str, industry: str, bump: int, facts: list[Fact], *, bars: bool = True) -> None:
        security = fresh_db.execute(
            """insert into security (name, mic, currency, country, primary_symbol, first_seen)
               values (%s, 'XNAS', 'USD', 'US', %s, '2020-01-01') returning id""",
            (f"{symbol} Inc", symbol),
        ).fetchone()[0]
        fresh_db.execute(
            """insert into security_symbol (security_id, symbol, mic, valid_from, source)
               values (%s, %s, 'XNAS', '2020-01-01', 'test')""",
            (security, symbol),
        )
        fresh_db.execute(
            """insert into security_sector (security_id, sector_node_id, valid_from, source)
               values (%s, %s, '2020-01-01', 'yfinance')""",
            (security, industries[industry]),
        )
        observation = an_observation(security)
        if bars:
            for offset in OFFSETS:
                close = Decimal(100 + bump) if offset == 0 else Decimal(100)
                fresh_db.execute(
                    """insert into price_daily
                       (security_id, trade_date, open, high, low, close, volume,
                        observed_at, ingest_observation_id)
                       values (%s, %s, %s, %s, %s, %s, 1, %s, %s)""",
                    (security, AS_OF - timedelta(days=offset), close, close, close, close,
                     SEEN, observation),
                )
        if facts:
            with fresh_db.cursor() as cur:
                insert_facts(
                    cur, security, observation, SEEN, facts,
                    latest_values(cur, security), metric_ids(cur),
                )
        ids[symbol] = security

    for i in range(20):
        company(f"S{i:02d}", "software", i, _facts(str(Decimal(1) + Decimal(i) / 10)))
    company("BANK", "banks-regional", 3, _facts("1.2"))
    company("EURO", "software", 8, _facts("1", currency="EUR"))
    company("NOBR", "software", 0, [], bars=False)

    ids["run"] = run_scoring(fresh_db, as_of=AS_OF).run_id
    return ids


def _reproduce(conn: Any, security_id: int, run_id: int) -> Reproduction:
    resolved = resolve_run(conn, run_id)
    assert resolved is not None
    run, _ = resolved
    industry = resolve(conn, [security_id], as_of=run.as_of)[security_id].industry
    return reproduce(conn, security_id=security_id, run=run, industry=industry)


def _stored(conn: Any, security_id: int) -> dict[str, Decimal]:
    return dict(conn.execute(
        """select m.code, md.raw_value
             from metric_daily md join metric m on m.id = md.metric_id
            where md.security_id = %s""",
        (security_id,),
    ).fetchall())


def test_every_stored_metric_reproduces(fresh_db, scored):
    compared = 0
    for symbol in [*(f"S{i:02d}" for i in range(20)), "BANK", "EURO"]:
        reproduction = _reproduce(fresh_db, scored[symbol], scored["run"])
        stored = _stored(fresh_db, scored[symbol])
        assert reproduction.values is not None, symbol
        assert reproduction.refreshed == frozenset(), symbol

        for code, raw in stored.items():
            assert reproduction.values[code] == raw, (symbol, code)
            compared += 1
        produced = {
            code for code, value in reproduction.values.items() if not isinstance(value, Absent)
        }
        assert produced == set(stored), symbol

    assert compared > 200


def test_the_view_stops_at_the_cutoff_for_a_run_that_started_after_it(fresh_db, scored):
    reproduction = _reproduce(fresh_db, scored["S00"], scored["run"])

    assert reproduction.visible_through == datetime(2026, 3, 3, 6, tzinfo=timezone.utc)


def test_a_bar_restamped_since_the_run_marks_prices_refreshed(fresh_db, scored):
    fresh_db.execute(
        """update price_daily set observed_at = now() + interval '1 hour'
            where security_id = %s and trade_date = %s""",
        (scored["S03"], AS_OF),
    )

    assert _reproduce(fresh_db, scored["S03"], scored["run"]).refreshed == {"price"}


def test_a_fact_restamped_since_the_run_marks_fundamentals_refreshed(fresh_db, scored):
    fresh_db.execute(
        """update fundamental_fact set observed_at = now() + interval '1 hour'
            where id = (select min(id) from fundamental_fact where security_id = %s)""",
        (scored["S03"],),
    )

    assert _reproduce(fresh_db, scored["S03"], scored["run"]).refreshed == {"fundamentals"}


def test_a_failure_inside_reproduction_is_reported_rather_than_raised(fresh_db, scored, monkeypatch, caplog):
    def unreadable(*args: object, **kwargs: object) -> None:
        raise RuntimeError("facts unreadable")

    monkeypatch.setattr("screener.screen.explain.read_facts", unreadable)

    reproduction = _reproduce(fresh_db, scored["S05"], scored["run"])

    assert reproduction.values is None
    assert "could not reproduce security" in caplog.text
