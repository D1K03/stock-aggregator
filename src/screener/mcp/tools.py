"""What the connector can do, and the SQL behind each of it.

Two general tools and four shaped ones. The general pair — `list_tables` and
`query` — are the whole database and are what makes an unanticipated question
answerable. The shaped four exist because the questions actually asked have
shapes, and "what has Reddit been saying about NVDA" should not require a model
to first discover that `social_item` joins nothing and is filtered on
`created_utc`.

Every shaped tool goes through `playground.select`, which takes a literal from
this file and binds its arguments as parameters. Nothing here concatenates a
value into a statement, so a ticker is a ticker and never a fragment of SQL.

`Sequence[Any]` params rather than an f-string is not merely the safe habit: the
tools take input from a model that is reading third-party Reddit text, which is
as close to attacker-controlled as this system gets.
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from screener import playground
from screener.mcp import config

logger = logging.getLogger(__name__)

# Caps chosen per tool rather than shared, because "recent comments" wants more
# rows than "which ingest runs failed" and both are bounded by the engine anyway.
MAX_TERM = 120
DEFAULT_DAYS = 7
MAX_DAYS = 365


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    run: Callable[[dict[str, Any]], str]


def _rows_as_text(result: playground.Result, note: str = "") -> str:
    """A result as compact JSON, which is what a model reads best.

    Columns and rows kept apart rather than zipped into objects per row, for the
    reason the HTTP endpoint gives: `select 1 as a, 2 as a` is legal SQL and an
    object would silently drop a column. It is also a third of the bytes.
    """
    payload: dict[str, Any] = {
        "columns": [c.name for c in result.columns],
        "types": [c.type for c in result.columns],
        "rows": [list(r) for r in result.rows],
        "row_count": result.row_count,
    }
    if result.truncated:
        payload["truncated"] = True
        payload["note"] = f"cut at {result.limit} rows; narrow the query for more"
    if note:
        payload["about"] = note
    text = json.dumps(payload, separators=(",", ":"))
    if len(text) > config.MAX_TOOL_CHARS:
        # The engine's own payload budget is sized for a browser table; a
        # context window is not that. Refusing beats silently handing back half
        # a result that reads as the whole one.
        return json.dumps(
            {
                "error": "that result is too large to return",
                "row_count": result.row_count,
                "hint": "add a narrower filter, fewer columns, or a smaller limit",
            }
        )
    return text


def _int(arguments: dict[str, Any], name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(arguments.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(low, min(value, high))


# -- the general pair --------------------------------------------------------


def _list_tables(_: dict[str, Any]) -> str:
    tables = playground.catalog()
    return json.dumps(
        {
            "tables": [
                {
                    "name": f"{t.schema}.{t.name}" if t.schema != "public" else t.name,
                    "columns": [f"{c.name} {c.type}" for c in t.columns],
                }
                for t in tables
            ]
        },
        separators=(",", ":"),
    )


def _query(arguments: dict[str, Any]) -> str:
    statement = str(arguments.get("sql") or "")
    limit = _int(arguments, "limit", playground.DEFAULT_ROWS, 1, playground.MAX_ROWS)
    try:
        return _rows_as_text(playground.run(statement, limit))
    except playground.QueryError as exc:
        message = exc.message
        if exc.sqlstate in ("42P01", "42703"):
            # Naming the set costs a few tokens once and saves a whole extra
            # round trip, which is the same trade `bot/tools/charts.py` makes.
            message += ". Call list_tables for what exists."
        return json.dumps({"error": message, "sqlstate": exc.sqlstate})


# -- the shaped four ---------------------------------------------------------


def _reddit_chatter(arguments: dict[str, Any]) -> str:
    term = str(arguments.get("term") or "").strip()[:MAX_TERM]
    if not term:
        return json.dumps({"error": "term is required"})
    days = _int(arguments, "days", DEFAULT_DAYS, 1, MAX_DAYS)
    limit = _int(arguments, "limit", 50, 1, playground.MAX_ROWS)
    # The wildcards live in the *parameter*, not the SQL, so the statement has
    # no literal `%` in it at all. That is not only tidier: psycopg scans a
    # parameterised statement for placeholders and a stray `%` raises before
    # Postgres is reached, which the engine would otherwise have to explain.
    result = playground.select(
        """
        select created_utc, subreddit, kind, author, score,
               coalesce(title, left(body, 400)) as text, permalink
          from social_item
         where created_utc > now() - make_interval(days => %s)
           and (title ilike %s or body ilike %s)
         order by created_utc desc
        """,
        [days, f"%{term}%", f"%{term}%"],
        limit,
    )
    return _rows_as_text(result, f"r/* mentions of {term!r} in the last {days} days")


def _price_history(arguments: dict[str, Any]) -> str:
    symbol = str(arguments.get("symbol") or "").strip().upper()[:20]
    if not symbol:
        return json.dumps({"error": "symbol is required"})
    days = _int(arguments, "days", 90, 1, 3650)
    result = playground.select(
        """
        select p.trade_date, p.open, p.high, p.low, p.close, p.volume
          from price_daily p
          join security_symbol s on s.security_id = p.security_id
         where s.symbol = %s and s.valid_to is null
           and p.trade_date > current_date - %s
         order by p.trade_date desc
        """,
        [symbol, days],
        _int(arguments, "limit", 200, 1, playground.MAX_ROWS),
    )
    if result.row_count == 0:
        return json.dumps(
            {
                "rows": [],
                "note": f"no stored bars for {symbol}. It may not be in the "
                "universe, or ingest may not have reached it yet.",
            }
        )
    return _rows_as_text(result, f"daily bars for {symbol}")


def _latest_transcripts(arguments: dict[str, Any]) -> str:
    limit = _int(arguments, "limit", 80, 1, playground.MAX_ROWS)
    session = arguments.get("session_id")
    if session is None:
        result = playground.select(
            """
            select t.captured_at, s.id as session_id, s.platform, s.channel,
                   s.title, s.state, t.text
              from skybird.transcript_segment t
              join skybird.stream_session s on s.id = t.session_id
             order by t.captured_at desc
            """,
            None,
            limit,
        )
    else:
        result = playground.select(
            """
            select t.captured_at, t.offset_seconds, t.text
              from skybird.transcript_segment t
             where t.session_id = %s
             order by t.seq desc
            """,
            [_int(arguments, "session_id", 0, 0, 2**31)],
            limit,
        )
    return _rows_as_text(result, "newest first; reverse them to read in order")


def _rupert_mentions(arguments: dict[str, Any]) -> str:
    """What resolved to a security, with how it read and what kind of claim it is.

    The shaped read over `rupert.mention` joined to the text it was decided
    about, because the join is the whole point of that table and writing it by
    hand every time is three tables and two left joins.
    """
    symbol = str(arguments.get("symbol") or "").strip().upper()[:20]
    days = _int(arguments, "days", DEFAULT_DAYS, 1, MAX_DAYS)
    limit = _int(arguments, "limit", 50, 1, playground.MAX_ROWS)
    state = str(arguments.get("state") or "resolved").strip().lower()
    if state not in {"resolved", "none", "unsure", "crowded", "failed", "any"}:
        return json.dumps({"error": "state must be resolved, none, unsure, crowded, failed or any"})

    clause = "" if state == "any" else "and m.state = %(state)s"
    symbol_clause = "and s.primary_symbol = %(symbol)s" if symbol else ""
    result = playground.select(
        """
        select m.id, si.created_utc, s.primary_symbol as symbol, m.state,
               m.confidence, m.claim_kind,
               r.positive - r.negative as tone,
               si.subreddit,
               left(coalesce(nullif(si.title, '') || ' ', '') || si.body, 300) as text
          from rupert.mention m
          join social_item si on si.id = m.social_item_id
          left join security s on s.id = m.security_id
          left join rupert.reading r on r.mention_id = m.id
         where si.created_utc > now() - make_interval(days => %(days)s)
        """
        + clause
        + symbol_clause
        + " order by si.created_utc desc",
        {"days": days, "state": state, "symbol": symbol},
        limit,
    )
    if result.row_count == 0:
        return json.dumps(
            {
                "rows": [],
                "note": "nothing resolved there. Rupert only decides about "
                "securities in the universe, and most of the corpus resolves to "
                "nothing at all — which is the layer working, not failing.",
            }
        )
    return _rows_as_text(
        result,
        "tone is FinBERT's positive minus negative; confidence is the decision "
        "model's and is a reading to threshold rather than a fact to trust",
    )


def _rupert_decision(arguments: dict[str, Any]) -> str:
    """One decision, whole: the text, the shortlist, every probability, the tone.

    The provenance behind a single row, which is the thing worth being able to
    ask for — a wrong link is this layer's failure mode and no aggregate shows
    you one.
    """
    mention_id = _int(arguments, "id", 0, 0, 2**31)
    if mention_id <= 0:
        return json.dumps({"error": "id is required; get one from rupert_mentions"})
    result = playground.select(
        """
        select m.id, m.state, m.rupert_version, m.candidates, m.chosen,
               m.confidence, m.probabilities,
               m.own_business, m.position_talk, m.injection,
               m.claim_kind, m.claim_confidence,
               m.model, m.input_tokens, m.cost_usd, m.observed_at,
               s.primary_symbol as symbol,
               si.subreddit, si.created_utc,
               coalesce(nullif(si.title, '') || ' ', '') || si.body as text,
               r.model as tone_model, r.positive, r.negative, r.neutral
          from rupert.mention m
          join social_item si on si.id = m.social_item_id
          left join security s on s.id = m.security_id
          left join rupert.reading r on r.mention_id = m.id
         where m.id = %s
        """,
        [mention_id],
        5,
    )
    if result.row_count == 0:
        return json.dumps({"error": f"no decision {mention_id}"})
    return _rows_as_text(
        result,
        "everything stored about one decision. `probabilities` is the full "
        "distribution the choice came from; the three noul columns are the gates",
    )


def _rupert_standings(arguments: dict[str, Any]) -> str:
    """Which securities the corpus is talking about, and how it reads."""
    days = _int(arguments, "days", 7, 1, MAX_DAYS)
    limit = _int(arguments, "limit", 25, 1, playground.MAX_ROWS)
    result = playground.select(
        """
        select s.primary_symbol as symbol, s.name,
               count(*) as mentions,
               count(r.id) as read_for_tone,
               round(avg(r.positive - r.negative)::numeric, 4) as mean_tone
          from rupert.mention m
          join social_item si on si.id = m.social_item_id
          join security s on s.id = m.security_id
          left join rupert.reading r on r.mention_id = m.id
         where m.state = 'resolved'
           and si.created_utc > now() - make_interval(days => %s)
         group by 1, 2
         order by mentions desc
        """,
        [days],
        limit,
    )
    return _rows_as_text(
        result,
        "mean_tone here is a plain average. The pillar uses a trimmed mean with "
        "a minimum item count, so a security with three mentions has a number "
        "here and would have none there",
    )


def _ingest_health(_: dict[str, Any]) -> str:
    result = playground.select(
        """
        select r.started_at, r.finished_at, r.status, d.code as source,
               r.endpoint, left(coalesce(r.error, ''), 200) as error
          from ingest_run r
          join data_source d on d.id = r.source_id
         order by r.started_at desc
        """,
        None,
        40,
    )
    return _rows_as_text(
        result, "most recent ingest runs; 'partial' or 'failed' means gaps in the data"
    )


# -- the registry ------------------------------------------------------------


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
    }


TOOLS: tuple[Tool, ...] = (
    Tool(
        "list_tables",
        "Every table this connector may read, with its columns and their types. "
        "Call this before writing SQL against an unfamiliar table.",
        _schema({}),
        _list_tables,
    ),
    Tool(
        "query",
        "Run one read-only SELECT against the screener database. Postgres syntax. "
        "One statement, no writes. Use this for anything the shaped tools do not "
        "already cover.",
        _schema(
            {
                "sql": {"type": "string", "description": "A single SELECT statement."},
                "limit": {
                    "type": "integer",
                    "description": f"Rows to return, up to {playground.MAX_ROWS}.",
                },
            },
            ["sql"],
        ),
        _query,
    ),
    Tool(
        "reddit_chatter",
        "Recent Reddit posts and comments mentioning a ticker or phrase, newest "
        "first, from the subreddits this screener follows.",
        _schema(
            {
                "term": {
                    "type": "string",
                    "description": "A ticker such as NVDA, or any phrase.",
                },
                "days": {"type": "integer", "description": "How far back. Default 7."},
                "limit": {"type": "integer", "description": "Rows. Default 50."},
            },
            ["term"],
        ),
        _reddit_chatter,
    ),
    Tool(
        "price_history",
        "Stored daily open/high/low/close/volume bars for one symbol, newest first.",
        _schema(
            {
                "symbol": {"type": "string", "description": "Ticker, e.g. AAPL."},
                "days": {"type": "integer", "description": "How far back. Default 90."},
                "limit": {"type": "integer", "description": "Rows. Default 200."},
            },
            ["symbol"],
        ),
        _price_history,
    ),
    Tool(
        "latest_transcripts",
        "Recent speech from live streams being captured, newest first. Omit "
        "session_id for everything across all captures.",
        _schema(
            {
                "session_id": {
                    "type": "integer",
                    "description": "One capture, from a previous result.",
                },
                "limit": {"type": "integer", "description": "Segments. Default 80."},
            }
        ),
        _latest_transcripts,
    ),
    Tool(
        "rupert_mentions",
        "Reddit comments Rupert linked to a security, with the tone FinBERT read "
        "and what kind of claim each makes. Omit symbol for everything.",
        _schema(
            {
                "symbol": {"type": "string", "description": "Ticker, e.g. NVDA."},
                "state": {
                    "type": "string",
                    "description": "resolved (default), none, unsure, crowded, "
                    "failed, or any.",
                },
                "days": {"type": "integer", "description": "How far back. Default 7."},
                "limit": {"type": "integer", "description": "Rows. Default 50."},
            }
        ),
        _rupert_mentions,
    ),
    Tool(
        "rupert_decision",
        "Everything behind one decision: the text, the shortlist it chose "
        "between, every probability it returned, the gates, what it cost, and "
        "the tone FinBERT read. Get an id from rupert_mentions.",
        _schema(
            {"id": {"type": "integer", "description": "A rupert.mention id."}},
            ["id"],
        ),
        _rupert_decision,
    ),
    Tool(
        "rupert_standings",
        "Which securities the Reddit corpus is talking about most, over a "
        "window, with how many were read for tone.",
        _schema(
            {
                "days": {"type": "integer", "description": "Window. Default 7."},
                "limit": {"type": "integer", "description": "Rows. Default 25."},
            }
        ),
        _rupert_standings,
    ),
    Tool(
        "ingest_health",
        "When each data source last ran and whether it succeeded. Check this "
        "before treating an absence of data as a fact about the world.",
        _schema({}),
        _ingest_health,
    ),
)

BY_NAME = {t.name: t for t in TOOLS}


def specs() -> list[dict[str, Any]]:
    """The `tools/list` payload."""
    return [
        {"name": t.name, "description": t.description, "inputSchema": t.schema}
        for t in TOOLS
    ]
