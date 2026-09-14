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
from screener.scoring import CODES, Absent, resolve, run_scoring
from screener.screen import (
    AmbiguousSymbol,
    Reproduction,
    RunChanged,
    UnknownSymbol,
    read_security,
    reproduce,
    resolve_run,
    security_params,
)

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


# -- the detail endpoint (D10, D14) ------------------------------------------


def detail(conn: Any, symbol: str, run: int | None = None) -> dict[str, Any]:
    query = {"symbol": [symbol]} | ({} if run is None else {"run": [str(run)]})
    return read_security(conn, security_params(query))


def metrics(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {metric["code"]: metric for pillar in payload["pillars"] for metric in pillar["metrics"]}


def statuses(payload: dict[str, Any]) -> dict[str, str]:
    return {code: metric["status"] for code, metric in metrics(payload).items()}


def test_a_standard_company_and_a_bank_expect_different_metrics(fresh_db, scored):
    standard, bank = detail(fresh_db, "S05"), detail(fresh_db, "BANK")

    assert [(p["key"], p["present"], p["expected"]) for p in standard["pillars"]] == [
        ("V", 3, 3), ("Q", 4, 4), ("M", 4, 4),
    ]
    assert [(p["key"], p["present"], p["expected"]) for p in bank["pillars"]] == [
        ("V", 2, 2), ("Q", 1, 1), ("M", 4, 4),
    ]
    assert set(metrics(bank)) == {"earnings_yield", "book_yield", "roe", *CODES}
    assert standard["sector"] == {"code": "technology", "name": "Technology"}
    assert standard["industry"] == {"code": "software", "name": "Software"}
    assert metrics(standard)["debt_to_equity"]["higher_is_better"] is False
    assert metrics(standard)["debt_to_equity"]["unit"] == "multiple"


def test_every_metric_of_an_untouched_night_is_ok_or_absent(fresh_db, scored):
    for symbol in ("S00", "S19", "BANK", "EURO"):
        assert set(statuses(detail(fresh_db, symbol)).values()) <= {"ok", "absent"}, symbol
    assert set(statuses(detail(fresh_db, "S00")).values()) == {"ok"}


def test_an_ok_metric_carries_both_values_as_exact_strings(fresh_db, scored):
    got = metrics(detail(fresh_db, "S07"))["ret_12m"]

    assert got["status"] == "ok"
    assert got["stored"]["raw"] == got["reproduced"] == "0.07"
    assert got["stored"]["peer_group"] == "Technology"
    assert got["stored"]["market_ranked"] is False


def test_an_absent_metric_says_why(fresh_db, scored):
    got = metrics(detail(fresh_db, "EURO"))["earnings_yield"]

    assert (got["status"], got["stored"], got["reproduced"]) == ("absent", None, None)
    assert "EUR" in got["reason"]


def test_a_stored_value_altered_since_the_run_is_a_mismatch(fresh_db, scored):
    fresh_db.execute(
        """update metric_daily set raw_value = raw_value + 1
            where security_id = %s
              and metric_id = (select id from metric where code = 'ret_12m')""",
        (scored["S07"],),
    )

    got = metrics(detail(fresh_db, "S07"))["ret_12m"]

    assert (got["status"], got["stored"]["raw"], got["reproduced"]) == ("mismatch", "1.07", "0.07")


def test_a_currency_corrected_since_the_run_makes_its_ratios_reproduce_unexpectedly(fresh_db, scored):
    # F15: scoring reads today's currency, so facts the run dropped as foreign now count.
    fresh_db.execute("update security set currency = 'EUR' where id = %s", (scored["EURO"],))

    got = statuses(detail(fresh_db, "EURO"))

    assert got["earnings_yield"] == got["roic"] == "unexpected"
    assert got["ret_12m"] == "ok"


def test_a_bar_restamped_since_the_run_leaves_quality_compared(fresh_db, scored):
    fresh_db.execute(
        """update price_daily set observed_at = now() + interval '1 hour'
            where security_id = %s and trade_date = %s""",
        (scored["S03"], AS_OF),
    )

    shown = detail(fresh_db, "S03")
    got = statuses(shown)

    assert {got[code] for code in CODES} == {"refreshed"}
    assert {got[code] for code in ("earnings_yield", "ebitda_ev", "fcf_yield")} == {"refreshed"}
    assert {got[code] for code in ("roic", "gross_margin", "debt_to_equity", "interest_cover")} == {"ok"}
    assert shown["reproduction"]["refreshed_inputs"] == ["price"]


def test_a_fact_restamped_since_the_run_leaves_momentum_compared(fresh_db, scored):
    fresh_db.execute(
        """update fundamental_fact set observed_at = now() + interval '1 hour'
            where id = (select min(id) from fundamental_fact where security_id = %s)""",
        (scored["S03"],),
    )

    got = statuses(detail(fresh_db, "S03"))

    assert {got[code] for code in CODES} == {"ok"}
    assert {status for code, status in got.items() if code not in CODES} == {"refreshed"}


def test_a_reproduction_that_raises_still_returns_the_stored_metrics(fresh_db, scored, monkeypatch):
    def unreadable(*args: object, **kwargs: object) -> None:
        raise RuntimeError("facts unreadable")

    monkeypatch.setattr("screener.screen.explain.read_facts", unreadable)

    shown = detail(fresh_db, "S05")

    assert set(statuses(shown).values()) == {"unchecked"}
    assert metrics(shown)["roic"]["stored"] is not None
    assert shown["score"] is not None


def test_the_run_and_its_builds_are_named(fresh_db, scored):
    shown = detail(fresh_db, "S05")

    assert (shown["scored"], shown["run_id"], shown["latest"]) == (True, scored["run"], True)
    assert shown["reproduction"]["visible_through"] == "2026-03-03T06:00:00+00:00"
    assert shown["reproduction"]["run_build"] == shown["reproduction"]["running_build"]
    assert shown["closes"][-1] == [AS_OF.isoformat(), "105.00"]


def test_a_security_with_nothing_scored_is_not_scored(fresh_db, scored):
    assert detail(fresh_db, "NOBR") == {
        "scored": False,
        "run_id": scored["run"],
        "latest": True,
        "symbol": "NOBR",
        "name": "NOBR Inc",
        "sector": {"code": "technology", "name": "Technology"},
        "active": True,
        "closes": [],
    }


def test_a_symbol_is_matched_regardless_of_case_or_a_dollar_sign(fresh_db, scored):
    assert detail(fresh_db, "$s05")["symbol"] == "S05"


def test_an_unknown_symbol_is_refused(fresh_db, scored):
    with pytest.raises(UnknownSymbol):
        detail(fresh_db, "ZZZZ")


def test_a_symbol_listed_on_two_exchanges_is_ambiguous(fresh_db, scored):
    other = fresh_db.execute(
        """insert into security (name, mic, currency, country, primary_symbol, first_seen)
           values ('S05 Other', 'XNYS', 'USD', 'US', 'S05', '2020-01-01') returning id"""
    ).fetchone()[0]
    fresh_db.execute(
        """insert into security_symbol (security_id, symbol, mic, valid_from, source)
           values (%s, 'S05', 'XNYS', '2020-01-01', 'test')""",
        (other,),
    )

    with pytest.raises(AmbiguousSymbol) as caught:
        detail(fresh_db, "S05")

    assert caught.value.exchanges == ("XNAS", "XNYS")


def test_a_pinned_run_that_does_not_exist_is_refused(fresh_db, scored):
    with pytest.raises(RunChanged):
        detail(fresh_db, "S05", run=999_999)


def test_the_detail_awaits_the_first_night_before_looking_for_the_symbol(fresh_db):
    assert detail(fresh_db, "ANY") == {"state": "awaiting_first_night"}
