"""The dashboard's concept data, mirrored for the agent.

**None of this is real.** It is invented, schema-shaped sample data so the
dashboard has something to draw before ingest exists. When ingest lands this
module is deleted and the chart tool reads `score_snapshot` instead; nothing
here should grow a second purpose in the meantime.

It mirrors `web/lib/data.ts`, which is a duplication and is worth explaining.
The dashboard renders these numbers server-side from TypeScript and the agent
runs in Python, and the two live in separate Docker build contexts, so neither
can import the other's copy. Sharing a JSON file would need a build step that
exists only for concept data. Instead both copies are plain, and
`tests/test_concept.py` parses the TypeScript and fails if they disagree — so
the duplication is checked rather than trusted. `series()` is a direct port of
`history()` and the same test pins its output.
"""

from dataclasses import dataclass
from datetime import date, timedelta

THRESHOLD = 75

# Points per series. Sixty trading-ish days, matching the dashboard's "60d".
SPAN = 60


@dataclass(frozen=True, slots=True)
class Row:
    sym: str
    name: str
    sector: str
    score: int
    prev: int
    pillars: tuple[int, ...]
    flags: tuple[str, ...] = ()


ROWS: tuple[Row, ...] = (
    Row("PGR", "Progressive", "Insurance", 84, 82, (76, 88, 82, 54, 79)),
    Row("NVDA", "NVIDIA", "Semiconductors", 82, 68, (44, 91, 88, 62, 71), ("Earnings 6d",)),
    Row("LLY", "Eli Lilly", "Healthcare", 79, 80, (29, 90, 83, 71, 55), ("FDA window",)),
    Row("AVGO", "Broadcom", "Semiconductors", 78, 75, (51, 84, 79, 58, 64)),
    Row("MSFT", "Microsoft", "Software", 76, 75, (38, 93, 71, 60, 57)),
    Row("JPM", "JPMorgan", "Banks", 74, 68, (66, 79, 77, 57, 62)),
    Row("COST", "Costco", "Retail", 72, 72, (22, 86, 74, 68, 61)),
    Row("MU", "Micron", "Semiconductors", 71, 73, (78, 55, 81, 66, 48)),
    Row("CAT", "Caterpillar", "Industrials", 68, 59, (62, 74, 78, 51, 83)),
    Row("AMD", "AMD", "Semiconductors", 66, 62, (49, 72, 69, 74, 52)),
    Row("XOM", "Exxon", "Energy", 63, 67, (82, 61, 42, 49, 58), ("Ex-div 3d",)),
    Row("DOW", "Dow", "Chemicals", 38, 45, (71, 41, 22, 35, 44), ("Guidance cut",)),
)

MEDIANS: dict[str, int] = {
    "Semiconductors": 57, "Insurance": 52, "Healthcare": 54, "Software": 55,
    "Banks": 51, "Retail": 53, "Industrials": 50, "Energy": 48, "Chemicals": 47,
}
PEERS: dict[str, int] = {
    "Semiconductors": 38, "Insurance": 24, "Healthcare": 31, "Software": 45,
    "Banks": 29, "Retail": 22, "Industrials": 33, "Energy": 27, "Chemicals": 21,
}

PILLAR_NAMES: tuple[str, ...] = ("Valuation", "Quality", "Momentum", "Sentiment", "Insider")


def find(ticker: str) -> Row | None:
    """A row by symbol or company name, case-insensitively.

    Names are matched as well as symbols because the alternative is a whole
    extra model round: asked to "chart Nvidia", a symbol-only lookup fails, the
    model apologises or guesses, and the question is paid for twice.
    """
    wanted = ticker.strip().lstrip("$").casefold()
    if not wanted:
        return None
    exact = next((r for r in ROWS if r.sym.casefold() == wanted), None)
    if exact:
        return exact
    return next((r for r in ROWS if r.name.casefold().startswith(wanted)), None)


def series(row: Row) -> list[float]:
    """The score history the dashboard draws, to the last decimal place.

    A port of `history()` in `web/lib/data.ts`, including its 32-bit linear
    congruential generator. The arithmetic is written to stay inside float64
    exactly as JavaScript's does — `seed * 1664525` peaks below 2**53 — so both
    languages produce identical values rather than merely similar ones. A chart
    in chat that disagreed with the chart on the page by a fraction would look
    like a bug in the data, not a rounding difference.
    """
    seed = 7
    for character in row.sym:
        seed = (seed * 31 + ord(character)) & 0xFFFFFFFF

    def rnd() -> float:
        nonlocal seed
        seed = (seed * 1664525 + 1013904223) & 0xFFFFFFFF
        return seed / 2**32

    out: list[float] = []
    value = row.prev - (row.score - row.prev) * 0.9 - 3 + rnd() * 6
    for i in range(SPAN - 1):
        drift = (row.prev - value) * (i / (SPAN - 1)) * 0.18
        value = min(97.0, max(8.0, value + drift + (rnd() - 0.5) * 3.2))
        out.append(value)
    # The walk arrives at yesterday, then today is appended, so the final step
    # is the move the alert fired on and the only jump in the line.
    out[SPAN - 2] = float(row.prev)
    out.append(float(row.score))
    return out


def dates(today: date | None = None) -> list[str]:
    """One ISO date per point, ending today.

    Sent with the chart so the browser is not counting back from a date of its
    own; two clocks disagreeing is how a marker ends up labelled with the wrong
    day.
    """
    end = today or date.today()
    return [(end - timedelta(days=SPAN - 1 - i)).isoformat() for i in range(SPAN)]
