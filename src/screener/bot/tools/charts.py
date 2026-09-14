"""The chart tool: Steven draws a security's adjusted price and marks a point on it.

Three decisions shape this module.

**The series never reaches the model.** Sixty points is more than the whole tool
budget, and a tool result is context on every subsequent round, so sending the
data would be paid for repeatedly to tell the model something it cannot read as
well as a chart can. The model gets a sentence; the chart travels beside the
reply through `collecting()` and is rendered by whoever asked.

**The model chooses the question, the data answers it.** `mark` names *what* to
find -- a peak, a surge -- and the index is then computed from the real series
here. Letting the model supply coordinates would be asking it to invent where a
marker goes, which is the same failure as inventing a number, and it would be
wrong in the most convincing possible way: drawn on a chart, precisely, in the
wrong place.

**The figures are real, and read as Steven.** The closes are the stored bars,
adjusted by scoring's own rule, and the caption's scores are the night the
dashboard serves. Both are read through `playground.select` as Steven's
read-only role, using `screener.screen`'s own statements and records, in three
reads (ui-swap spec D17). Before this the chart worked without a database; now
it cannot, and that is the point.
"""

import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from screener import playground, screen
from screener.bot.tools.registry import tool
from screener.scoring import LOGIC_DESCRIPTION

logger = logging.getLogger(__name__)

# The longest run "biggest surge" is allowed to span. Without a ceiling the
# answer is almost always the whole window, which is a trend rather than a
# surge; with one the label can say how many days it took and be checked.
RUN_DAYS = 10

# What `mark` accepts. Listed in the tool description so the model picks from
# the set rather than guessing a word this module will not recognise. No
# crossing: that needs score history and alerting, and neither exists yet.
MARKS = ("peak", "low", "surge", "drop", "latest")

UNAVAILABLE = "error: the chart is unavailable because the database cannot be reached."


@dataclass(frozen=True, slots=True)
class Mark:
    """Something drawn on the line: one point, or a span between two."""

    kind: str  # "point" | "span"
    index: int
    label: str
    end: int | None = None
    tone: str = "copper"


@dataclass(frozen=True, slots=True)
class Chart:
    """An adjusted price series, ready to draw. Sent to the browser, never to the model."""

    ticker: str
    title: str
    subtitle: str
    series: tuple[Decimal, ...]
    dates: tuple[str, ...]
    marks: tuple[Mark, ...] = field(default_factory=tuple)

    def payload(self) -> dict[str, object]:
        """The shape sent to the browser and to the renderer.

        `kind` is "price", so the renderer draws the data's own range and no
        score threshold or median (D16). Closes travel as floats here, unlike
        the screen endpoints' decimal strings: they are coordinates for a line,
        and D5's precision is already in the labels, written from the Decimals.
        """
        return {
            "kind": "price",
            "ticker": self.ticker,
            "title": self.title,
            "subtitle": self.subtitle,
            "series": [float(v) for v in self.series],
            "dates": list(self.dates),
            "marks": [
                {
                    "kind": m.kind, "index": m.index, "label": m.label,
                    "end": m.end, "tone": m.tone,
                }
                for m in self.marks
            ],
        }


# Charts produced while answering one question. A ContextVar rather than a
# module global because `agent._think` runs on a worker thread per request:
# `asyncio.to_thread` copies the context, so two people asking at once cannot
# be handed each other's charts.
_PENDING: ContextVar[list[Chart] | None] = ContextVar("pending_charts", default=None)


@contextmanager
def collecting(enabled: bool = True) -> Iterator[list[Chart]]:
    """Gather whatever the tools drew while answering one question.

    `enabled=False` still yields a list, always empty. It is how a surface that
    cannot render a chart says so: nothing is collected, and the tool sees that
    and does not tell the model a chart is on screen.
    """
    charts: list[Chart] = []
    token = _PENDING.set(charts if enabled else None)
    try:
        yield charts
    finally:
        _PENDING.reset(token)


def _day(day: date) -> str:
    """`2026-09-05` as `5 Sep`. Short, because it goes in a label and a prompt."""
    return f"{day.day} {day:%b}"


def _peak(values: Sequence[Decimal]) -> int:
    return max(range(len(values)), key=lambda i: values[i])


def _low(values: Sequence[Decimal]) -> int:
    return min(range(len(values)), key=lambda i: values[i])


def _percent(start: Decimal, end: Decimal) -> Decimal:
    return (end / start - 1) * 100


def _run(values: Sequence[Decimal], rising: bool) -> tuple[int, int]:
    """The steepest percent rise (or fall) over at most `RUN_DAYS`, as (start, end).

    Percent rather than price points, so a surge means the same thing for a
    $20 stock as for a $2,000 one.
    """
    best = (0, 0)
    best_move = Decimal(0)
    for start in range(len(values)):
        if values[start] <= 0:
            continue
        for end in range(start + 1, min(start + RUN_DAYS, len(values) - 1) + 1):
            move = _percent(values[start], values[end])
            if (move > best_move) if rising else (move < best_move):
                best_move, best = move, (start, end)
    return best


def annotate(
    mark: str, values: Sequence[Decimal], days: Sequence[date]
) -> tuple[Mark | None, str]:
    """Turn a requested mark into something drawn and something said.

    Returns the annotation and the one-line answer for the model, so the label
    on the chart and the sentence in the reply are computed once from the same
    numbers and cannot disagree.
    """
    if mark == "peak":
        i = _peak(values)
        return (
            Mark("point", i, f"peak {screen.price(values[i])}", tone="copper"),
            f"Peak {screen.price(values[i])} on {_day(days[i])}.",
        )
    if mark == "low":
        i = _low(values)
        return (
            Mark("point", i, f"low {screen.price(values[i])}", tone="blue"),
            f"Low {screen.price(values[i])} on {_day(days[i])}.",
        )
    if mark in ("surge", "drop"):
        start, end = _run(values, rising=mark == "surge")
        if end == start:
            return None, f"No meaningful {mark} in this window."
        move = f"{_percent(values[start], values[end]):+.1f}% over {end - start}d"
        return (
            Mark("span", start, move, end=end, tone="copper" if mark == "surge" else "blue"),
            f"Biggest {mark} {move}, {_day(days[start])} to {_day(days[end])}.",
        )
    if mark == "latest":
        i = len(values) - 1
        return (
            Mark("point", i, f"now {screen.price(values[i])}", tone="copper"),
            f"Latest {screen.price(values[i])} on {_day(days[i])}.",
        )
    return None, ""


def _caption(match: screen.ChartSecurityRow) -> str:
    """The scores on the screen's night, as the dashboard would show them (D17)."""
    if match.run_id is None or match.as_of is None:
        return "not yet scored"
    if match.score is None:
        return f"not scored on {_day(match.as_of)}"
    pillars = " ".join(
        f"{key} {screen.one_decimal(value) or '—'}"
        for key, value in (("V", match.v_score), ("Q", match.q_score), ("M", match.m_score))
    )
    partial = (
        " · partial"
        if match.min_coverage is not None and screen.partial(match.min_coverage)
        else ""
    )
    return f"score {screen.one_decimal(match.score)} on {_day(match.as_of)} · {pillars}{partial}"


def _security(symbol: str) -> screen.ChartSecurityRow:
    """Read one: the symbol's matches with the screen's night, then the one meant."""
    found = playground.select(
        screen.CHART_SECURITY, {"symbol": symbol, "logic": LOGIC_DESCRIPTION}
    )
    return screen.choose_match(symbol, screen.parse_all(screen.ChartSecurityRow, found.rows))


def _closes(security_id: int, as_of: date) -> list[tuple[date, Decimal]]:
    """Reads two and three: bars and corporate actions, adjusted as the dashboard adjusts them.

    No `observed_at` bound, for the reason `screen.read` gives (D11): ingest
    re-stamps the last week of bars nightly.
    """
    bind = {
        "ids": [security_id],
        "as_of": as_of,
        "since": as_of - timedelta(days=screen.CLOSE_LOOKBACK_DAYS),
        "count": screen.CHART_CLOSES,
    }
    bars = screen.parse_all(
        screen.BarRow, playground.select(screen.CLOSES, bind, screen.CHART_CLOSES).rows
    )
    actions = screen.parse_all(screen.ActionRow, playground.select(screen.ACTIONS, bind).rows)
    return [
        (date.fromisoformat(day), Decimal(close))
        for day, close in screen.closes(bars, actions, screen.CHART_CLOSES)
    ]


@tool(
    "chart",
    "Draw a ticker's 60-day adjusted price. mark: " + "|".join(MARKS) + " marks that point.",
)
def chart(ticker: str, mark: str = "") -> str:
    """Register a chart and describe it in one line.

    The return value is what the model reads. It carries the figures worth
    stating in a sentence and nothing else, because the reader can see the rest.
    """
    wanted = mark.strip().lower()
    if wanted and wanted not in MARKS:
        return f"error: mark must be one of {'|'.join(MARKS)}"
    symbol = ticker.strip().removeprefix("$").upper()
    if not symbol or len(symbol) > screen.MAX_SYMBOL:
        return "error: give a ticker symbol, such as NVDA"
    if not playground.enabled():
        return UNAVAILABLE

    try:
        match = _security(symbol)
        if not match.is_active:
            return f"{match.symbol} is no longer in the universe."
        # With no scored night yet, the prices up to today (plan C2).
        closes = _closes(match.security_id, match.as_of or datetime.now(timezone.utc).date())
    except (playground.NotConfigured, playground.Unavailable):
        return UNAVAILABLE
    except screen.UnknownSymbol:
        return f"{symbol} is not a current symbol in the universe."
    except screen.AmbiguousSymbol as exc:
        return f"{symbol} is listed on {', '.join(exc.exchanges)}; say which exchange."

    caption = _caption(match)
    if len(closes) < 2:
        return f"{match.symbol} has no stored prices to draw yet; {caption}."
    days = [day for day, _ in closes]
    values = [close for _, close in closes]
    annotation, answer = annotate(wanted, values, days)

    charts = _PENDING.get()
    if charts is None:
        # Nothing is collecting, so the chart would be drawn and dropped. The
        # model still gets the figures rather than an error it has to explain.
        logger.info("chart(%s) called outside a collecting context", match.symbol)
    else:
        charts.append(
            Chart(
                ticker=match.symbol,
                title=f"{match.symbol} — adjusted close, {len(values)} trading days",
                subtitle=f"{match.name} · {caption}",
                series=tuple(values),
                dates=tuple(day.isoformat() for day in days),
                marks=(annotation,) if annotation else (),
            )
        )

    head = (
        f"{match.symbol} ({match.name}) adjusted close over {len(values)} trading days "
        f"to {_day(days[-1])}: latest {screen.price(values[-1])}; {caption}."
    )
    # Only claim a chart exists where one is actually rendered, so the model
    # never refers the reader to something that is not there.
    tail = (
        "The chart is shown to them; answer in a sentence, do not list the numbers."
        if charts is not None
        else ""
    )
    return " ".join(part for part in (head, answer, tail) if part)
