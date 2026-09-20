"""Reading the corpora and writing decisions. Never opens a socket.

The other half of the split `screener.reddit` and `screener.edgar` both draw:
`decide` talks to the model and never to the database, this talks to the
database and never to the model. Keeping that visible is what lets either half
be tested without the other being reachable.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from screener.rupert.decide import Choice
from screener.rupert.version import VERSION
from screener.sentiment import Sentiment

logger = logging.getLogger(__name__)

SOURCE_CODE = "rupert"

SOCIAL = "social"
DOCUMENT = "document"


@dataclass(frozen=True, slots=True)
class Text:
    """One piece of text to resolve, read out of whichever corpus holds it.

    The shared dataclass between the two halves. `corpus` and `item_id` are what
    the mention is keyed on; `at` is the clock the walk advances along, and it
    is the item's own timestamp rather than when we read it -- a comment written
    on Tuesday is evidence about Tuesday whenever we get to it.
    """

    corpus: str
    item_id: int
    at: datetime
    title: str
    body: str

    @property
    def content(self) -> str:
        """Title and body as one string, which is what a candidate is found in.

        A Reddit post keeps its subject in the title and a comment has none, so
        searching only the body would miss the one place a post is most likely
        to name the company it is about.
        """
        return f"{self.title}\n{self.body}".strip() if self.title else self.body


def source_id(conn: psycopg.Connection, code: str = SOURCE_CODE) -> int:
    with conn.cursor() as cur:
        cur.execute("select id from data_source where code = %s", [code])
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"data_source {code!r} is missing; run migrations")
    return int(row[0])


def lexicon(conn: psycopg.Connection) -> dict[str, str]:
    """Every current symbol mapped to its company name, upper-cased.

    Read once per pass and handed to `candidates` as data, which is what keeps
    that module pure -- the same move `screener.edgar` makes in passing the
    universe's CIKs to `source.transactions` as an argument.

    **Current symbols only** (`valid_to is null`), and active securities only.
    `security_symbol` is bitemporal and a departed security's row is left open
    by `universe load`, so matching without both conditions would resolve
    comments to companies that have left the universe and cannot be scored.

    A symbol held by two active securities keeps the first by id and says so.
    `security_symbol_current_uq` makes that impossible per MIC, so this only
    fires for the same ticker on two exchanges -- rare, and a warning beats
    whichever row the planner happened to return.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select upper(sy.symbol), s.name, s.id
            from security_symbol sy
            join security s on s.id = sy.security_id
            where sy.valid_to is null and s.is_active
            order by s.id
            """
        )
        rows = cur.fetchall()
    out: dict[str, str] = {}
    for symbol, name, _security_id in rows:
        if symbol in out:
            logger.warning("symbol %s is held by more than one active security", symbol)
            continue
        out[str(symbol)] = str(name)
    return out


def securities(conn: psycopg.Connection) -> dict[str, int]:
    """Every current symbol mapped to its `security.id`.

    The companion to `lexicon`, kept separate because the two are wanted at
    different moments: the names go into a question and the ids come back out of
    an answer, and bundling them would put a database key into a prompt.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select upper(sy.symbol), min(s.id)
            from security_symbol sy
            join security s on s.id = sy.security_id
            where sy.valid_to is null and s.is_active
            group by 1
            """
        )
        return {str(symbol): int(security_id) for symbol, security_id in cur.fetchall()}


def read_through(conn: psycopg.Connection, corpus: str) -> datetime | None:
    """How far along this corpus the resolver has read, or None if never.

    The frontier, and a scalar rather than a set because the unit of work is a
    position on a continuous timeline. `screener.edgar` keeps a set in
    `ingest_run` because its unit is a named day; the argument for not deriving
    either from stored rows is identical, and spelled out on `rupert.progress`.
    """
    with conn.cursor() as cur:
        cur.execute("select read_through from rupert.progress where corpus = %s", [corpus])
        row = cur.fetchone()
    return row[0] if row else None


def advance(
    conn: psycopg.Connection, corpus: str, *, through: datetime, items: int
) -> None:
    """Move the frontier forward, and count what was examined getting there.

    Never backwards: a pass that read less than the last one -- because the
    batch was smaller, or because it was interrupted -- must not re-offer what
    has already been decided. `greatest` makes that structural rather than
    something every caller has to remember.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into rupert.progress (corpus, read_through, items_read)
            values (%s, %s, %s)
            on conflict (corpus) do update set
                read_through = greatest(rupert.progress.read_through, excluded.read_through),
                items_read = rupert.progress.items_read + excluded.items_read,
                updated_at = now()
            """,
            [corpus, through, items],
        )


def unread_social(
    conn: psycopg.Connection, *, after: datetime, limit: int
) -> list[Text]:
    """The next social items past the frontier, oldest first.

    Forward through time, which is the opposite of how `screener.reddit` walks
    and for the opposite reason. The ingest walks backwards because the mirror
    pages that way and the newest comment matters most; this walks forwards
    because a high-water mark only means anything if nothing is ever skipped,
    and a backwards walk would leave a hole behind it every time the batch was
    smaller than the day.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, created_utc, coalesce(title, ''), body
            from social_item
            where created_utc > %s
            order by created_utc
            limit %s
            """,
            [after, limit],
        )
        return [
            Text(corpus=SOCIAL, item_id=int(r[0]), at=r[1], title=str(r[2]), body=str(r[3]))
            for r in cur.fetchall()
        ]


def unread_documents(
    conn: psycopg.Connection, *, after: datetime, limit: int
) -> list[Text]:
    """The next scraped articles past the frontier, oldest first.

    `first_seen_at` rather than `published`, which is nullable and is the
    publisher's claim rather than ours. The frontier has to advance on a clock
    that cannot be null and cannot move backwards when a site lies about a date.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, first_seen_at, title, text
            from magpie.document
            where first_seen_at > %s
            order by first_seen_at
            limit %s
            """,
            [after, limit],
        )
        return [
            Text(corpus=DOCUMENT, item_id=int(r[0]), at=r[1], title=str(r[2]), body=str(r[3]))
            for r in cur.fetchall()
        ]


def save_mention(
    conn: psycopg.Connection,
    source: int,
    text: Text,
    *,
    state: str,
    candidates: Sequence[str],
    security_id: int | None = None,
    choice: Choice | None = None,
    nouls: Mapping[str, float] | None = None,
    claim: Choice | None = None,
    model: str = "",
    input_tokens: int = 0,
    cost_usd: float = 0.0,
) -> int:
    """Write one decision and return its id.

    Idempotent on the item, not on the decision. Re-resolving an item under a
    new prompt or a new model version is an `update`, because the question
    "which security is this text about" has one current answer and a second row
    would make a join ambiguous for every reader afterwards. What is *not*
    overwritten is the reading: `rupert.reading` keys on the model, so tone read
    under old weights survives a re-resolution.
    """
    columns = {
        "source_id": source,
        "social_item_id": text.item_id if text.corpus == SOCIAL else None,
        "document_id": text.item_id if text.corpus == DOCUMENT else None,
        "security_id": security_id,
        "state": state,
        "candidates": list(candidates),
        "chosen": choice.chosen if choice else None,
        "confidence": choice.confidence if choice else None,
        "probabilities": Jsonb(dict(choice.probabilities)) if choice else None,
        "own_business": (nouls or {}).get("own_business"),
        "position_talk": (nouls or {}).get("position_talk"),
        "injection": (nouls or {}).get("injection"),
        "claim_kind": claim.chosen if claim else None,
        "claim_confidence": claim.confidence if claim else None,
        "model": model,
        "input_tokens": input_tokens,
        "cost_usd": cost_usd,
        # Which pipeline asked, not which model answered — `model` above is the
        # second of those. Stamped here rather than defaulted in SQL so a row
        # written by code that predates a bump cannot inherit the new number.
        "rupert_version": VERSION,
        "observed_at": datetime.now(tz=text.at.tzinfo),
    }
    conflict = "social_item_id" if text.corpus == SOCIAL else "document_id"
    fixed = {"source_id", conflict}

    # Composed with `psycopg.sql` rather than an f-string, which is the rule
    # CLAUDE.md states: psycopg types a query as `LiteralString`, so SQL
    # assembled at runtime is rejected by design and composition is how a
    # statement that genuinely varies gets built. What varies here is only which
    # of the two corpus keys the conflict is on -- the alternative is two
    # near-identical eighteen-column statements that drift apart on the first
    # column anybody adds to one of them.
    statement = sql.SQL(
        "insert into rupert.mention ({names}) values ({places}) "
        "on conflict (source_id, {key}) do update set {updates} returning id"
    ).format(
        names=sql.SQL(", ").join(sql.Identifier(name) for name in columns),
        places=sql.SQL(", ").join(sql.Placeholder() for _ in columns),
        key=sql.Identifier(conflict),
        updates=sql.SQL(", ").join(
            sql.SQL("{col} = excluded.{col}").format(col=sql.Identifier(name))
            for name in columns
            if name not in fixed
        ),
    )
    with conn.cursor() as cur:
        cur.execute(statement, list(columns.values()))
        row = cur.fetchone()
    # Always a row: the `do update` carries no `where`, so unlike
    # `screener.reddit.store.save` -- where an unchanged hash is excluded and
    # returns nothing -- there is no path here that writes neither.
    assert row is not None
    return int(row[0])


def save_reading(
    conn: psycopg.Connection, mention_id: int, reading: Sentiment, *, model: str
) -> None:
    """Write how one resolved mention reads, under a named model.

    `do nothing` rather than `do update`: a reading under the same weights is
    the same reading, and re-reading a corpus is a row per new model rather than
    an edit. That is `fundamental_fact`'s rule -- a fact is appended, never
    rewritten -- applied to the one thing here that is a measurement rather than
    a decision.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into rupert.reading (mention_id, positive, negative, neutral, model)
            values (%s, %s, %s, %s, %s)
            on conflict (mention_id, model) do nothing
            """,
            [mention_id, reading.positive, reading.negative, reading.neutral, model],
        )


def calls_today(conn: psycopg.Connection, source: int) -> int:
    """How many decisions have been asked for since midnight UTC.

    The meter, and it counts *attempts* rather than successes. A call that
    failed may still have been served and billed, and a budget that only counts
    what came back cleanly is a budget a failing endpoint can talk you past.
    `crowded` is excluded because no request was made for it.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select count(*) from rupert.mention
            where source_id = %s
              and observed_at >= date_trunc('day', now() at time zone 'utc')
              and state <> 'crowded'
            """,
            [source],
        )
        row = cur.fetchone()
    return int(row[0]) if row else 0


def paused(conn: psycopg.Connection) -> tuple[bool, str | None, datetime | None]:
    """Whether a pass may run, who said so, and when.

    Read at the top of every pass rather than at container start, so a pause
    takes effect on the next wake rather than on the next deploy — which is the
    whole point of it being a row instead of an environment variable.
    """
    with conn.cursor() as cur:
        cur.execute("select paused, changed_by, changed_at from rupert.control")
        row = cur.fetchone()
    return (bool(row[0]), row[1], row[2]) if row else (False, None, None)


def set_paused(conn: psycopg.Connection, value: bool, *, by: str) -> None:
    """Stop or start the nightly passes. Attributed, always."""
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into rupert.control (only_row, paused, changed_by, changed_at)
            values (true, %s, %s, now())
            on conflict (only_row) do update set
                paused = excluded.paused,
                changed_by = excluded.changed_by,
                changed_at = excluded.changed_at
            """,
            [value, by],
        )


def last_pass(conn: psycopg.Connection, source: int) -> datetime | None:
    """When the last pass finished, which is what the next one is timed from.

    `finished_at`, not `started_at`: the container sleeps its interval *after* a
    pass, so a long pass pushes the next one out and a clock anchored on the
    start would promise a run that is already late.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select max(finished_at) from ingest_run
            where source_id = %s and finished_at is not null
            """,
            [source],
        )
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def spent_today(conn: psycopg.Connection, source: int) -> float:
    """What the decisions have actually cost since midnight UTC.

    The companion to `calls_today`, and the one that holds when the count does
    not: a count bounds spend only while the unit price is what we assumed, and
    this reads what OpenRouter billed. Summed over every state including the
    failures, because a call that failed may still have been served and paid for.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select coalesce(sum(cost_usd), 0) from rupert.mention
            where source_id = %s
              and observed_at >= date_trunc('day', now() at time zone 'utc')
            """,
            [source],
        )
        row = cur.fetchone()
    return float(row[0]) if row else 0.0


def mentions_by_day(
    conn: psycopg.Connection, security_id: int, *, through: date, days: int
) -> list[int]:
    """One resolved-mention count per day for one security, oldest first.

    The baseline `reduce.attention` measures tonight against. Days with no
    mentions are returned as zeros rather than omitted, because a silent day is
    the most common day for most of the universe and dropping it would make
    every security look permanently busy.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select coalesce(counted.n, 0)
            from generate_series(
                %(through)s::date - (%(days)s - 1), %(through)s::date, interval '1 day'
            ) as day
            left join (
                select date_trunc('day', si.created_utc)::date as on_day, count(*) as n
                from rupert.mention m
                join social_item si on si.id = m.social_item_id
                where m.security_id = %(security)s and m.state = 'resolved'
                  and si.created_utc >= %(through)s::date - (%(days)s - 1)
                  and si.created_utc < %(through)s::date + 1
                group by 1
            ) as counted on counted.on_day = day::date
            order by day
            """,
            {"security": security_id, "through": through, "days": days},
        )
        return [int(r[0]) for r in cur.fetchall()]


def tones_for(
    conn: psycopg.Connection, security_id: int, *, on: date, model: str
) -> list[float]:
    """Every `positive - negative` for one security on one day, under one model.

    The input to `reduce.mood`. The subtraction happens here rather than in SQL
    only in the sense that the columns are handed over whole -- the definition
    of `score` stays in `screener.sentiment.Sentiment`, so there is exactly one
    of it.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select r.positive, r.negative
            from rupert.reading r
            join rupert.mention m on m.id = r.mention_id
            join social_item si on si.id = m.social_item_id
            where m.security_id = %s and m.state = 'resolved' and r.model = %s
              and si.created_utc >= %s::date and si.created_utc < %s::date + 1
            """,
            [security_id, model, on, on],
        )
        return [float(positive) - float(negative) for positive, negative in cur.fetchall()]


def start_run(conn: psycopg.Connection, source: int, endpoint: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into ingest_run (source_id, endpoint, started_at, status)
            values (%s, %s, now(), 'running') returning id
            """,
            [source, endpoint],
        )
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


def finish_run(
    conn: psycopg.Connection,
    run_id: int,
    status: str,
    *,
    requested: int | None = None,
    ok: int | None = None,
    error: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            update ingest_run set
                finished_at = now(), status = %s,
                securities_requested = %s, securities_ok = %s, error = %s
            where id = %s
            """,
            [status, requested, ok, error[:500] if error else None, run_id],
        )
