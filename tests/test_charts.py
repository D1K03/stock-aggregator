"""The chart tool: Steven draws a security's adjusted price from real data.

Three things are defended here:
- The mark Steven puts on a chart is computed from the series, never chosen by a
  model. A marker in the wrong place is a lie told precisely.
- The figures are the stored bars, adjusted by scoring's own rule and read as
  Steven's read-only role, captioned with the scores the screen serves
  (ui-swap spec D17).
- A chart still reaches Discord as the same payload the browser draws.

The database fixtures are written here rather than borrowed from
`test_screen_read.py`: this repository keeps no test-helper modules, and Steven
needs a sliver of that world -- a few securities, sixty-odd bars, one night.
"""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx
import pytest
from psycopg.conninfo import make_conninfo

from screener import playground
from screener.bot import render
from screener.bot.tools import TOOLS, Chart, dispatch
from screener.bot.tools.charts import MARKS, UNAVAILABLE, annotate, collecting
from screener.scoring import LOGIC_DESCRIPTION

AS_OF = date(2026, 3, 2)
PASSWORD = "throwaway-for-this-test"


def weekdays(last: date, count: int) -> list[date]:
    """`count` weekdays ending on `last`, oldest first."""
    days: list[date] = []
    day = last
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return days[::-1]


def draw(ticker: str, mark: str = "") -> tuple[str, list[Chart]]:
    with collecting() as drawn:
        said = dispatch("chart", {"ticker": ticker, "mark": mark})
    return said, list(drawn)


# -- marks, on a fixed series ------------------------------------------------

# A rise of 20 from 200 (+10%) and a rise of 10 from 50 (+20%): the surge is
# the second, because a move is measured in percent, not in price points.
SERIES = [Decimal(v) for v in ("200", "220", "215", "50", "55", "60", "58", "57", "56", "55")]
DAYS = weekdays(AS_OF, len(SERIES))


def _day(day: date) -> str:
    return f"{day.day} {day:%b}"


def test_the_tool_offers_price_marks_and_never_a_crossing():
    description = TOOLS["chart"].description
    assert "adjusted price" in description
    for mark in MARKS:
        assert mark in description
    assert "crossing" not in description and "crossing" not in MARKS


def test_a_peak_and_a_low_are_found_in_the_series_and_labelled_as_prices():
    peak, said = annotate("peak", SERIES, DAYS)
    low, _ = annotate("low", SERIES, DAYS)

    assert peak is not None and (peak.kind, peak.index, peak.label) == ("point", 1, "peak 220.00")
    assert said == f"Peak 220.00 on {_day(DAYS[1])}."
    assert low is not None and (low.index, low.label) == (3, "low 50.00")


def test_a_surge_is_the_steepest_percent_rise_and_says_how_long_it_took():
    surge, said = annotate("surge", SERIES, DAYS)

    assert surge is not None and (surge.kind, surge.index, surge.end) == ("span", 3, 5)
    assert surge.label == "+20.0% over 2d"
    assert said == f"Biggest surge +20.0% over 2d, {_day(DAYS[3])} to {_day(DAYS[5])}."


def test_a_drop_is_the_steepest_percent_fall():
    drop, _ = annotate("drop", SERIES, DAYS)

    assert drop is not None and (drop.index, drop.end, drop.label) == (1, 3, "-77.3% over 2d")


def test_latest_marks_the_last_close():
    latest, _ = annotate("latest", SERIES, DAYS)

    assert latest is not None and (latest.index, latest.label) == (9, "now 55.00")


def test_a_close_of_a_thousand_or_more_is_labelled_without_decimals():
    peak, _ = annotate("peak", [Decimal("1500"), Decimal("1601")], DAYS[:2])

    assert peak is not None and peak.label == "peak 1601"


def test_a_flat_series_has_no_surge_to_mark():
    assert annotate("surge", [Decimal("100")] * 5, DAYS[:5]) == (
        None, "No meaningful surge in this window.",
    )


def test_an_unknown_mark_is_refused_before_anything_is_read():
    for mark in ("wibble", "crossing"):
        said, drawn = draw("ABC", mark)
        assert drawn == [] and said.startswith("error: mark must be one of")


def test_the_payload_is_a_price_chart_with_no_score_lines():
    chart = Chart(
        ticker="ABC", title="t", subtitle="s",
        series=(Decimal("272.10"), Decimal("1500")), dates=("2026-02-27", "2026-03-02"),
    )

    body = json.loads(json.dumps(chart.payload()))

    assert body["kind"] == "price"
    assert body["series"] == [272.1, 1500.0]
    assert "median" not in body and "threshold" not in body


# -- the tool, against the database as Steven's role -------------------------


@pytest.fixture
def steven(fresh_db, db_url, monkeypatch):
    """Steven's read-only role, provisioned as `test_playground.py` provisions it:
    through `ensure_password`, so what is tested is the role that ships."""
    monkeypatch.setenv("PLAYGROUND_DB_PASSWORD", PASSWORD)
    monkeypatch.setenv("PLAYGROUND_BOT_DB_PASSWORD", PASSWORD)
    playground.ensure_password(fresh_db)
    monkeypatch.setenv(
        "PLAYGROUND_DATABASE_URL",
        make_conninfo(db_url, user="playground_bot", password=PASSWORD),
    )
    return fresh_db


@dataclass
class Market:
    # `Any`, because the fixture's connection is untyped and a typed one would make
    # every `fetchone()[0]` below an optional-subscript error.
    conn: Any
    observe: Callable[[int], int]

    def security(
        self, symbol: str, *, active: bool = True, mic: str = "XNAS", name: str | None = None
    ) -> int:
        security_id = self.conn.execute(
            """insert into security
               (name, mic, currency, country, primary_symbol, first_seen, is_active)
               values (%s, %s, 'USD', 'US', %s, '2020-01-01', %s) returning id""",
            (name or f"{symbol} Inc", mic, symbol, active),
        ).fetchone()[0]
        self.conn.execute(
            """insert into security_symbol (security_id, symbol, mic, valid_from, source)
               values (%s, %s, %s, '2020-01-01', 'test')""",
            (security_id, symbol, mic),
        )
        return security_id

    def bars(self, security_id: int, closes: Sequence[str], *, last: date = AS_OF) -> list[date]:
        """One bar per weekday ending on `last`, each fetched the evening it closed."""
        observation = self.observe(security_id)
        days = weekdays(last, len(closes))
        for day, close in zip(days, closes):
            value = Decimal(close)
            self.conn.execute(
                """insert into price_daily
                   (security_id, trade_date, open, high, low, close, volume, observed_at,
                    ingest_observation_id)
                   values (%s, %s, %s, %s, %s, %s, 1, %s, %s)""",
                (security_id, day, value, value, value, value,
                 datetime.combine(day, time(22), tzinfo=timezone.utc), observation),
            )
        return days

    def split(self, security_id: int, effective: date, ratio: str) -> None:
        self.conn.execute(
            """insert into corporate_action
               (security_id, effective_date, action_type, ratio, amount, currency,
                observed_at, ingest_observation_id)
               values (%s, %s, 'split', %s, null, 'USD', %s, %s)""",
            (security_id, effective, Decimal(ratio),
             datetime.combine(effective, time(22), tzinfo=timezone.utc),
             self.observe(security_id)),
        )

    def run(self, as_of: date = AS_OF) -> int:
        started = datetime.combine(as_of, time(23), tzinfo=timezone.utc)
        return self.conn.execute(
            """insert into scoring_run
               (as_of_range, cutoff_offset, logic_version_id, weight_version_id, status,
                emits_alerts, git_sha, config_hash, started_at, finished_at, outcome)
               select daterange(%(as_of)s, %(next)s, '[)'), interval '30 hours', l.id, w.id,
                      'live', false, 'abc1234', '\\x9f3a'::bytea, %(started)s, %(started)s, 'ok'
                 from scoring_logic_version l, weight_version w
                where l.description = %(logic)s and w.code = 'v2'
               returning id""",
            {"as_of": as_of, "next": as_of + timedelta(days=1), "started": started,
             "logic": LOGIC_DESCRIPTION},
        ).fetchone()[0]

    def snapshot(
        self, run_id: int, security_id: int, score: str, *,
        min_coverage: str = "1", pillars: dict[str, str], as_of: date = AS_OF,
    ) -> None:
        self.conn.execute(
            """insert into snapshot_daily
               (as_of, scoring_run_id, security_id, blended_score, pillar_agreement,
                min_coverage, worst_fallback_level)
               values (%s, %s, %s, %s, 1, %s, 1)""",
            (as_of, run_id, security_id, Decimal(score), Decimal(min_coverage)),
        )
        for code, pillar_score in pillars.items():
            self.conn.execute(
                """insert into pillar_score_daily
                   (as_of, scoring_run_id, security_id, pillar_id, score, metric_count, coverage)
                   select %s, %s, %s, p.id, %s, 1, 1 from pillar p where p.code = %s""",
                (as_of, run_id, security_id, Decimal(pillar_score), code),
            )


@pytest.fixture
def market(steven, an_observation) -> Market:
    return Market(steven, an_observation)


def test_a_scored_security_is_drawn_from_its_adjusted_closes_with_its_scores(market):
    security = market.security("ABC")
    market.bars(security, [str(100 + i) for i in range(70)])
    run = market.run()
    market.snapshot(
        run, security, "71.2", min_coverage="0.5", pillars={"valuation": "44.5", "momentum": "80"}
    )

    said, drawn = draw("$abc", "latest")

    (chart,) = drawn
    assert chart.ticker == "ABC"
    assert chart.title == "ABC — adjusted close, 60 trading days"
    assert len(chart.series) == 60 and chart.series[-1] == Decimal("169.00")
    assert chart.dates[-1] == AS_OF.isoformat()
    assert chart.subtitle == "ABC Inc · score 71.2 on 2 Mar · V 44.5 Q — M 80.0 · partial"
    assert chart.marks[0].label == "now 169.00"
    assert "169.00" in said and "score 71.2" in said
    assert "llustrative" not in said


def test_before_any_scored_night_the_prices_are_still_drawn(market):
    today = datetime.now(timezone.utc).date()
    market.bars(market.security("ABC"), ["10", "11", "12"], last=today)

    said, drawn = draw("ABC")

    (chart,) = drawn
    assert chart.series == (Decimal("10.00"), Decimal("11.00"), Decimal("12.00"))
    assert "not yet scored" in chart.subtitle and "not yet scored" in said


def test_a_security_the_night_did_not_score_says_so(market):
    market.bars(market.security("ABC"), ["10", "11"])
    market.run()

    _, drawn = draw("ABC")

    (chart,) = drawn
    assert chart.subtitle == "ABC Inc · not scored on 2 Mar"


def test_a_split_inside_the_window_leaves_the_line_continuous(market):
    security = market.security("SPLT")
    days = market.bars(security, ["100"] * 10 + ["50"] * 10)
    market.split(security, days[10], "2")
    market.run()

    _, drawn = draw("SPLT")

    (chart,) = drawn
    assert set(chart.series) == {Decimal("50.00")}


def test_a_security_that_left_the_universe_is_not_drawn(market):
    market.bars(market.security("GONE", active=False), ["10", "11"])

    assert draw("GONE") == ("GONE is no longer in the universe.", [])


def test_an_unknown_symbol_is_not_drawn(market):
    assert draw("ZZZZ") == ("ZZZZ is not a current symbol in the universe.", [])


def test_a_reused_symbol_draws_the_security_trading_under_it_now(market):
    market.bars(market.security("ABC", active=False, mic="XNYS", name="ABC Old"), ["999", "999"])
    market.bars(market.security("ABC"), ["10", "11"])
    market.run()

    _, drawn = draw("ABC")

    (chart,) = drawn
    assert chart.series[-1] == Decimal("11.00")
    assert chart.subtitle.startswith("ABC Inc")


def test_a_chart_costs_at_most_three_reads(market, monkeypatch):
    security = market.security("ABC")
    market.bars(security, ["10", "11"])
    market.snapshot(market.run(), security, "50", pillars={"momentum": "50"})
    seen: list[str] = []
    real = playground.select

    def counted(*args: Any, **kwargs: Any) -> Any:
        seen.append(args[0])
        return real(*args, **kwargs)

    monkeypatch.setattr(playground, "select", counted)

    draw("ABC", "peak")

    assert len(seen) == 3


def test_without_a_role_the_chart_says_the_database_cannot_be_reached(monkeypatch):
    monkeypatch.delenv("PLAYGROUND_DATABASE_URL", raising=False)

    assert draw("ABC") == (UNAVAILABLE, [])


def test_an_unreachable_database_says_the_same(monkeypatch):
    monkeypatch.setenv("PLAYGROUND_DATABASE_URL", "postgresql://nobody:hunter2@127.0.0.1:1/none")

    assert draw("ABC") == (UNAVAILABLE, [])


def test_a_surface_that_cannot_draw_is_not_told_a_chart_is_shown(market):
    market.bars(market.security("ABC"), ["10", "12"])
    market.run()

    with collecting(False) as drawn:
        said = dispatch("chart", {"ticker": "ABC", "mark": "peak"})

    assert drawn == []
    assert "chart is shown" not in said
    assert "Peak 12.00" in said


def test_charts_do_not_leak_between_questions(market):
    market.bars(market.security("ABC"), ["10", "11"])
    market.bars(market.security("XYZ"), ["20", "21"])
    market.run()

    _, first = draw("ABC")
    _, second = draw("XYZ")

    assert [c.ticker for c in first] == ["ABC"]
    assert [c.ticker for c in second] == ["XYZ"]


# -- rasterising for Discord -------------------------------------------------


def _chart(ticker: str = "ABC") -> Chart:
    return Chart(
        ticker=ticker, title=f"{ticker} — adjusted close, 2 trading days", subtitle="s",
        series=(Decimal("10.00"), Decimal("11.00")), dates=("2026-02-27", "2026-03-02"),
    )


def test_a_chart_is_posted_to_the_renderer_as_its_own_payload():
    # The same JSON the browser receives, so the PNG cannot be drawn from a
    # different shape than the one on screen.
    sent: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n" + b"0" * 40)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        png = render.chart_png(_chart(), client=client)

    assert png is not None and png.startswith(b"\x89PNG")
    assert sent == _chart().payload()


def test_a_renderer_that_fails_costs_the_picture_not_the_answer():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="nope")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert render.chart_png(_chart(), client=client) is None


def test_something_that_is_not_a_png_is_refused():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html="<!doctype html><title>Sign in</title>")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert render.chart_png(_chart(), client=client) is None


def test_no_more_than_three_charts_are_attached_to_one_reply():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n")

    charts = tuple(_chart(symbol) for symbol in ("NVDA", "AMD", "MU", "JPM", "CAT"))
    original = httpx.Client
    try:
        httpx.Client = lambda **kw: original(transport=httpx.MockTransport(handler))  # type: ignore[assignment]
        drawn_files = render.chart_pngs(charts)
    finally:
        httpx.Client = original  # type: ignore[assignment]

    assert len(drawn_files) == render.MAX_CHARTS
    # Named for the ticker, so a saved image says what it is.
    assert drawn_files[0][0] == "nvda.png"
