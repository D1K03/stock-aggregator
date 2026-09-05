"""The SQL tool: Steven reads the database the playground exposes.

Two decisions, and they are the chart tool's decisions applied to a different
shape of answer.

**Rows go to the surface, not through the model.** A tool result is context on
every subsequent round, so a wide table would be paid for repeatedly to tell the
model something the reader can see better in a table. It travels beside the
reply through `collecting_rows()` and the browser renders it.

**Unless the answer is small enough to say.** A chart is never a scalar; a query
often is. "How many securities are there" wants the number in the sentence, and
a one-cell artifact would be silly, so a handful of cells goes inline.

There is no companion `tables` tool. `charts` already showed the cheaper move:
name the set in the *error*, on the round the model got it wrong, rather than
paying for a second tool spec on every message forever.
"""

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator

from screener import playground
from screener.bot.tools.registry import tool

logger = logging.getLogger(__name__)

# Above this the result is shown rather than said. A few cells read naturally in
# a sentence; a table does not.
INLINE_CELLS = 6

# What Steven asks for when he does not say. Smaller than the page's default:
# he is summarising, and the reader can widen it in the editor.
ROWS = 50


@dataclass(frozen=True, slots=True)
class Rows:
    """A result set, ready to render. Sent to the browser, never to the model."""

    sql: str
    columns: tuple[str, ...]
    types: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    truncated: bool
    ms: int

    def payload(self) -> dict[str, Any]:
        return {
            "sql": self.sql,
            "columns": [
                {"name": n, "type": t} for n, t in zip(self.columns, self.types)
            ],
            "rows": [list(r) for r in self.rows],
            "truncated": self.truncated,
            "ms": self.ms,
        }


# A ContextVar rather than a module global, for the reason `charts` gives:
# `agent._think` runs on a worker thread per request and `asyncio.to_thread`
# copies the context, so two people asking at once cannot be handed each other's
# results.
_PENDING: ContextVar[list[Rows] | None] = ContextVar("pending_rows", default=None)


@contextmanager
def collecting_rows(enabled: bool = True) -> Iterator[list[Rows]]:
    """Gather what the tool selected while answering one question.

    A second ContextVar rather than generalising the chart one. They have almost
    nothing in common — a chart is rasterised to PNG for Discord and a table
    cannot be rendered there at all — and generalising two things on the second
    instance is how an abstraction ends up fitting neither.
    """
    rows: list[Rows] = []
    token = _PENDING.set(rows if enabled else None)
    try:
        yield rows
    finally:
        _PENDING.reset(token)


def _known_tables() -> str:
    try:
        return " ".join(f"{t.schema}.{t.name}" for t in playground.catalog())
    except Exception:
        return ""


@tool(
    "sql",
    "Read-only SQL over the screener database. One SELECT. Unknown names return the list.",
)
def sql(query: str) -> str:
    """Run one query and describe it, or hand back the result to be shown.

    The return value is what the model reads. It carries the figures worth
    stating in a sentence and nothing else.
    """
    if not playground.enabled():
        return "error: the database console is not configured on this deployment"
    try:
        result = playground.run(query, ROWS)
    except playground.QueryError as exc:
        message = f"error: {exc.message}"
        if exc.sqlstate in ("42P01", "42703"):
            # Naming the set costs tokens once and saves a whole extra round of
            # the model guessing another name.
            tables = _known_tables()
            if tables:
                message += f". Tables: {tables}"
        return message
    except Exception as exc:
        logger.warning("sql tool failed: %s", type(exc).__name__)
        return f"error: could not reach the database ({type(exc).__name__})"

    names = tuple(c.name for c in result.columns)
    cells = result.row_count * max(1, len(names))

    if cells <= INLINE_CELLS:
        # Small enough to say. The reader wants the number in the sentence.
        flat = "; ".join(
            ", ".join(f"{n}={v}" for n, v in zip(names, row)) for row in result.rows
        )
        return flat or "no rows"

    pending = _PENDING.get()
    if pending is None:
        # Nothing is collecting, so a table would be built and dropped. Discord
        # is this case: give the model a compact rendering instead, which the
        # registry truncates.
        head = " | ".join(names)
        body = "\n".join(" | ".join(str(v) for v in row) for row in result.rows[:10])
        return f"{result.row_count} rows\n{head}\n{body}"

    pending.append(
        Rows(
            sql=query.strip(),
            columns=names,
            types=tuple(c.type for c in result.columns),
            rows=result.rows,
            truncated=result.truncated,
            ms=result.ms,
        )
    )
    return (
        f"rows={result.row_count} cols={len(names)} ms={result.ms}. "
        "Shown to them as a table; say what it means, do not read it out."
    )
