"""The chart tool: Steven draws a score history and marks a point on it.

Two decisions shape this module.

**The series never reaches the model.** Sixty points is more than the whole tool
budget, and a tool result is context on every subsequent round, so sending the
data would be paid for repeatedly to tell the model something it cannot read as
well as a chart can. The model gets a sentence; the chart travels beside the
reply through `collecting()` and is rendered by whoever asked.

**The model chooses the question, the data answers it.** `mark` names *what* to
find — a peak, a surge, a threshold crossing — and the index is then computed
from the real series here. Letting the model supply coordinates would be asking
it to invent where a marker goes, which is the same failure as inventing a
number, and it would be wrong in the most convincing possible way: drawn on a
chart, precisely, in the wrong place.
"""

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date

from screener import concept
from screener.bot.tools.registry import tool

logger = logging.getLogger(__name__)

# The longest run "biggest surge" is allowed to span. Without a ceiling the
# answer is almost always the whole window, which is a trend rather than a
# surge; with one the label can say how many days it took and be checked.
RUN_DAYS = 10

# What `mark` accepts. Listed in the tool description so the model picks from
# the set rather than guessing a word this module will not recognise.
MARKS = ("peak", "low", "surge", "drop", "crossing", "latest")


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
    """A score history, ready to draw. Sent to the browser, never to the model."""

    ticker: str
    title: str
    subtitle: str
    series: tuple[float, ...]
    dates: tuple[str, ...]
    median: float
    threshold: float
    marks: tuple[Mark, ...] = field(default_factory=tuple)

    def payload(self) -> dict[str, object]:
        """The shape sent to the browser.

        Values are rounded to two decimals. The chart is under 200px tall over
        a range of roughly sixty points, so a hundredth is far below a pixel,
        and full float repr would triple the size of the response for digits
        nobody can see.
        """
        return {
            "ticker": self.ticker,
            "title": self.title,
            "subtitle": self.subtitle,
            "series": [round(v, 2) for v in self.series],
            "dates": list(self.dates),
            "median": self.median,
            "threshold": self.threshold,
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
    cannot render a chart — Discord — says so: nothing is collected, and the
    tool sees that and does not tell the model a chart is on screen.
    """
    charts: list[Chart] = []
    token = _PENDING.set(charts if enabled else None)
    try:
        yield charts
    finally:
        _PENDING.reset(token)


def _day(iso: str) -> str:
    """`2026-09-05` as `5 Sep`. Short, because it goes in a label and a prompt."""
    parsed = date.fromisoformat(iso)
    return f"{parsed.day} {parsed:%b}"


def _peak(values: list[float]) -> int:
    return max(range(len(values)), key=lambda i: values[i])


def _low(values: list[float]) -> int:
    return min(range(len(values)), key=lambda i: values[i])


def _run(values: list[float], rising: bool) -> tuple[int, int]:
    """The steepest rise (or fall) over at most `RUN_DAYS`, as (start, end)."""
    best = (0, 0)
    best_move = 0.0
    for start in range(len(values)):
        for end in range(start + 1, min(start + RUN_DAYS, len(values) - 1) + 1):
            move = values[end] - values[start]
            if (move > best_move) if rising else (move < best_move):
                best_move, best = move, (start, end)
    return best


def _crossing(values: list[float], threshold: float) -> int | None:
    """The last time the line changed sides of the threshold, if it ever did."""
    found = None
    for i in range(1, len(values)):
        if (values[i - 1] < threshold) != (values[i] < threshold):
            found = i
    return found


def _annotate(
    mark: str, values: list[float], days: list[str], threshold: float
) -> tuple[Mark | None, str]:
    """Turn a requested mark into something drawn and something said.

    Returns the annotation and the one-line answer for the model, so the label
    on the chart and the sentence in the reply are computed once from the same
    numbers and cannot disagree.
    """
    if mark == "peak":
        i = _peak(values)
        return (
            Mark("point", i, f"peak {values[i]:.0f}", tone="copper"),
            f"Peak {values[i]:.0f} on {_day(days[i])}.",
        )
    if mark == "low":
        i = _low(values)
        return (
            Mark("point", i, f"low {values[i]:.0f}", tone="blue"),
            f"Low {values[i]:.0f} on {_day(days[i])}.",
        )
    if mark in ("surge", "drop"):
        start, end = _run(values, rising=mark == "surge")
        move = values[end] - values[start]
        span_days = end - start
        if span_days == 0:
            return None, f"No meaningful {mark} in this window."
        return (
            Mark(
                "span", start, f"{move:+.1f} over {span_days}d", end=end,
                tone="copper" if mark == "surge" else "blue",
            ),
            f"Biggest {mark} {move:+.1f} over {span_days}d, "
            f"{_day(days[start])} to {_day(days[end])}.",
        )
    if mark == "crossing":
        i = _crossing(values, threshold)
        if i is None:
            return None, f"It never crossed {threshold:.0f} in this window."
        direction = "up" if values[i] > values[i - 1] else "down"
        return (
            Mark("point", i, f"crossed {threshold:.0f} {direction}", tone="amber"),
            f"Crossed {threshold:.0f} {direction} on {_day(days[i])}.",
        )
    if mark == "latest":
        i = len(values) - 1
        return (
            Mark("point", i, f"now {values[i]:.0f}", tone="copper"),
            f"Latest {values[i]:.0f} on {_day(days[i])}.",
        )
    return None, ""


@tool(
    "chart",
    "Draw a ticker's 60-day score history. mark: " + "|".join(MARKS) + " marks that point.",
)
def chart(ticker: str, mark: str = "") -> str:
    """Register a chart and describe it in one line.

    The return value is what the model reads. It carries the figures worth
    stating in a sentence and nothing else, because the reader can see the rest.
    """
    row = concept.find(ticker)
    if row is None:
        # Naming the set costs a few tokens once and saves a whole extra round
        # of the model guessing another symbol.
        return (
            f"error: no ticker {ticker!r}. Have: "
            + " ".join(r.sym for r in concept.ROWS)
        )

    wanted = mark.strip().lower()
    if wanted and wanted not in MARKS:
        return f"error: mark must be one of {'|'.join(MARKS)}"

    values = concept.series(row)
    days = concept.dates()
    median = concept.MEDIANS[row.sector]
    peers = concept.PEERS[row.sector]
    annotation, answer = _annotate(wanted, values, days, concept.THRESHOLD)

    charts = _PENDING.get()
    if charts is None:
        # Nothing is collecting, so the chart would be drawn and dropped. The
        # model still gets the figures rather than an error it has to explain.
        logger.info("chart(%s) called outside a collecting context", row.sym)
    else:
        charts.append(
            Chart(
                ticker=row.sym,
                title=f"{row.sym} — blended score, 60d",
                subtitle=(
                    f"{row.sector} · {peers} peers · alert threshold "
                    f"{concept.THRESHOLD} · illustrative data"
                ),
                series=tuple(values),
                dates=tuple(days),
                median=float(median),
                threshold=float(concept.THRESHOLD),
                marks=(annotation,) if annotation else (),
            )
        )

    head = (
        f"{row.sym} {row.sector} 60d: now {row.score} (was {row.prev}), "
        f"median {median}, alert {concept.THRESHOLD}, {peers} peers."
    )
    # Only claim a chart exists where one is actually rendered. Discord shows
    # text, so telling the model a chart is on screen there would have it refer
    # the reader to something that is not.
    tail = "Illustrative concept data, not market data."
    if charts is not None:
        tail += " The chart is shown to them; answer in a sentence, do not list the numbers."
    return " ".join(part for part in (head, answer, tail) if part)
