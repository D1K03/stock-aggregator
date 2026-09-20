"""What the dashboard shows about Rupert. Reads only, never writes.

The same split `screener.screen` draws against `screener.scoring`: the pass
writes, this reads it back for a page. Kept out of `store` because the two want
different things — `store` reads to decide, in batches, on a frontier; this
reads to be looked at, in windows, aggregated. Mixing them would put a `limit 8`
for a table next to the query that decides what a night resolves.

**Two clocks, and they are not the same clock.** A mention has the item's own
timestamp (when the comment was written) and its own `observed_at` (when we
decided about it). Activity and cost are keyed on `observed_at`, because this
page answers "what did Rupert do, and what did it cost" — an operational
question about the resolver. Tone and attention are keyed on the item's day,
because those answer "what was being said about this security", which is a
question about the corpus. A single clock for both would make a backlog being
drained look like a day of feverish posting.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import psycopg

from screener.rupert.reduce import BASELINE_DAYS, Mood, attention, mood
from screener.rupert.reduce import Reading as ToneReading

logger = logging.getLogger(__name__)

# How many days the activity chart covers. Thirty is the shape of a month
# without being a calendar month, so the chart does not change width in
# February.
WINDOW_DAYS = 30

# How many days of the corpus the leaderboard reads. A week rather than a night,
# because the measured corpus gives only 57 securities ten mentions in three
# days — a single night's table would be mostly empty and would read as the
# resolver being broken rather than as the universe being quiet.
LEADERBOARD_DAYS = 7

# How many securities the leaderboard holds. Bounded because the tone query
# below fetches every reading for each of them.
LEADERBOARD_SIZE = 12

# How many decisions the review list holds. **This list is the point of the
# page.** A wrong link is this layer's failure mode and the only way to catch
# one is to read some, so the page shows the actual sentences and what was
# decided about them — including the refusals, on `magpie.attempt`'s terms.
REVIEW_SIZE = 25

# How many days of mentions one scored point is computed over.
#
# **Trailing, not daily, and that is forced by the corpus rather than chosen.**
# `reduce.mood` refuses a night under `MIN_MENTIONS` because a reading from three
# comments is one person's opinion; measured, only the very busiest securities
# clear ten mentions in a single day, so a strictly daily line would be blank for
# almost everything and would read as the scorer being broken rather than as the
# corpus being thin. Seven days pooled is the smallest window that gives most of
# the leaderboard a line at all, and it is stated on the chart rather than
# hidden — a trailing number presented as a daily one is a lie about how fast it
# can move.
ROLLING_DAYS = 7


@dataclass(frozen=True, slots=True)
class Day:
    """One day of the resolver's own work."""

    day: date
    decisions: int
    resolved: int
    cost_usd: Decimal


@dataclass(frozen=True, slots=True)
class Scored:
    """One day of one security: what was said, and what it scored.

    `tone` is null on a day whose trailing window did not reach the floor. That
    is a real state and the chart draws it as a gap rather than a zero — zero is
    what a genuinely balanced week looks like, and the two must not be the same
    mark.
    """

    day: date
    mentions: int
    window_mentions: int
    tone: Decimal | None


@dataclass(frozen=True, slots=True)
class Standing:
    """One security's week: how much was said, and how it read."""

    security_id: int
    symbol: str
    name: str
    mentions: int
    read: int
    mood: Mood | None
    attention: Decimal | None


@dataclass(frozen=True, slots=True)
class Decided:
    """One decision, with the text it was made about."""

    id: int
    state: str
    chosen: str | None
    symbol: str | None
    confidence: Decimal | None
    candidates: tuple[str, ...]
    claim_kind: str | None
    injection: Decimal | None
    position_talk: Decimal | None
    tone: Decimal | None
    subreddit: str | None
    excerpt: str
    at: datetime
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class Frontier:
    """How far through one corpus the resolver has read."""

    corpus: str
    read_through: datetime
    items_read: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Pass:
    """One recorded pass, from `ingest_run`."""

    endpoint: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    shortlisted: int | None
    resolved: int | None
    error: str | None


@dataclass(frozen=True, slots=True)
class Spend:
    """What the resolver has cost, against what it is allowed."""

    calls_today: int
    cost_today: Decimal
    cost_window: Decimal
    decisions_total: int
    per_decision: Decimal | None


@dataclass(frozen=True, slots=True)
class Reading:
    """One FinBERT reading of one mention."""

    model: str
    positive: Decimal
    negative: Decimal
    neutral: Decimal
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class Full:
    """One decision with everything that went into it and came out of it."""

    id: int
    state: str
    rupert_version: str
    corpus: str
    subreddit: str | None
    author: str | None
    permalink: str | None
    created_utc: datetime | None
    title: str | None
    body: str
    sent_text: str
    candidates: tuple[str, ...]
    chosen: str | None
    confidence: Decimal | None
    probabilities: dict[str, float] | None
    own_business: Decimal | None
    position_talk: Decimal | None
    injection: Decimal | None
    claim_kind: str | None
    claim_confidence: Decimal | None
    model: str
    input_tokens: int
    cost_usd: Decimal
    observed_at: datetime
    security_id: int | None
    symbol: str | None
    name: str | None
    readings: tuple[Reading, ...]


def decision(conn: psycopg.Connection, mention_id: int) -> Full | None:
    """One decision, whole. Every column, the text, and every reading of it.

    One query for the decision and one for its readings, rather than a join that
    would repeat the comment body once per model that has read it.
    """
    from screener.rupert.candidates import excerpt
    from screener.rupert.config import DEFAULT_MAX_CHARS

    with conn.cursor() as cur:
        cur.execute(
            """
            select m.id, m.state, m.rupert_version,
                   si.subreddit, si.author, si.permalink, si.created_utc,
                   si.title, si.body,
                   m.candidates, m.chosen, m.confidence, m.probabilities,
                   m.own_business, m.position_talk, m.injection,
                   m.claim_kind, m.claim_confidence,
                   m.model, m.input_tokens, m.cost_usd, m.observed_at,
                   m.security_id, s.primary_symbol, s.name
            from rupert.mention m
            join social_item si on si.id = m.social_item_id
            left join security s on s.id = m.security_id
            where m.id = %s
            """,
            [mention_id],
        )
        row = cur.fetchone()
    if row is None:
        return None

    with conn.cursor() as cur:
        cur.execute(
            """
            select model, positive, negative, neutral, observed_at
            from rupert.reading where mention_id = %s order by observed_at
            """,
            [mention_id],
        )
        readings = tuple(
            Reading(str(r[0]), Decimal(r[1]), Decimal(r[2]), Decimal(r[3]), r[4])
            for r in cur.fetchall()
        )

    title, body = (str(row[7]) if row[7] else None), str(row[8])
    content = f"{title}\n{body}".strip() if title else body
    return Full(
        id=int(row[0]), state=str(row[1]), rupert_version=str(row[2]),
        corpus="social",
        subreddit=str(row[3]) if row[3] else None,
        author=str(row[4]) if row[4] else None,
        permalink=str(row[5]) if row[5] else None,
        created_utc=row[6], title=title, body=body,
        # What was actually sent, after the trim — not the whole comment, which
        # is what a reader would otherwise assume the model saw.
        sent_text=excerpt(content, DEFAULT_MAX_CHARS),
        candidates=tuple(str(c) for c in (row[9] or ())),
        chosen=str(row[10]) if row[10] else None,
        confidence=Decimal(row[11]) if row[11] is not None else None,
        probabilities=row[12],
        own_business=Decimal(row[13]) if row[13] is not None else None,
        position_talk=Decimal(row[14]) if row[14] is not None else None,
        injection=Decimal(row[15]) if row[15] is not None else None,
        claim_kind=str(row[16]) if row[16] else None,
        claim_confidence=Decimal(row[17]) if row[17] is not None else None,
        model=str(row[18]), input_tokens=int(row[19]),
        cost_usd=Decimal(row[20]), observed_at=row[21],
        security_id=int(row[22]) if row[22] else None,
        symbol=str(row[23]) if row[23] else None,
        name=str(row[24]) if row[24] else None,
        readings=readings,
    )


def names_for(conn: psycopg.Connection, symbols: Sequence[str]) -> dict[str, str]:
    """Company names for a shortlist, so the question can be rebuilt as it was."""
    if not symbols:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            select upper(sy.symbol), s.name
            from security_symbol sy join security s on s.id = sy.security_id
            where sy.valid_to is null and upper(sy.symbol) = any(%s)
            """,
            [[s.upper() for s in symbols]],
        )
        return {str(a): str(b) for a, b in cur.fetchall()}


def counts(conn: psycopg.Connection) -> dict[str, int]:
    """How many decisions of each state exist, all time.

    Every state, including the ones that link nothing. A page that showed only
    `resolved` would say the layer is working perfectly at the exact moment it
    had started refusing everything.
    """
    with conn.cursor() as cur:
        cur.execute("select state, count(*) from rupert.mention group by 1")
        found = {str(state): int(n) for state, n in cur.fetchall()}
    with conn.cursor() as cur:
        cur.execute("select count(*) from rupert.reading")
        row = cur.fetchone()
    found["read"] = int(row[0]) if row else 0
    return found


def spend(conn: psycopg.Connection, *, days: int = WINDOW_DAYS) -> Spend:
    """What has been spent today and over the window.

    `calls_today` is the same count the pass meters itself against, asked the
    same way, so the number on the page and the number that stops the pass
    cannot disagree — the failure `screener.bot.budget` describes, where every
    test stubbed the query out and the cap silently failed open.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select
                count(*) filter (
                    where observed_at >= date_trunc('day', now() at time zone 'utc')
                      and state <> 'crowded'
                ),
                coalesce(sum(cost_usd) filter (
                    where observed_at >= date_trunc('day', now() at time zone 'utc')
                ), 0),
                coalesce(sum(cost_usd) filter (
                    where observed_at >= now() - make_interval(days => %(days)s)
                ), 0),
                count(*) filter (where state <> 'crowded'),
                coalesce(sum(cost_usd), 0)
            from rupert.mention
            """,
            {"days": days},
        )
        row = cur.fetchone()
    if row is None:
        return Spend(0, Decimal(0), Decimal(0), 0, None)
    calls_today, cost_today, cost_window, decisions, cost_all = row
    return Spend(
        calls_today=int(calls_today),
        cost_today=Decimal(cost_today),
        cost_window=Decimal(cost_window),
        decisions_total=int(decisions),
        # The number worth watching, and the one that would move if a prompt
        # grew: the unit price is fixed, so cost per decision only changes when
        # the state or the questions get longer.
        per_decision=(Decimal(cost_all) / decisions) if decisions else None,
    )


def daily(conn: psycopg.Connection, *, days: int = WINDOW_DAYS) -> list[Day]:
    """The resolver's own work, per day, oldest first.

    Keyed on `observed_at` — see the module docstring. Days on which nothing was
    decided come back as zeros rather than being omitted, so a gap in the chart
    is a gap in the work rather than a gap in the axis.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select day::date,
                   coalesce(counted.decisions, 0),
                   coalesce(counted.resolved, 0),
                   coalesce(counted.cost, 0)
            from generate_series(
                (now() at time zone 'utc')::date - (%(days)s - 1),
                (now() at time zone 'utc')::date,
                interval '1 day'
            ) as day
            left join (
                select date_trunc('day', observed_at at time zone 'utc')::date as on_day,
                       count(*) filter (where state <> 'crowded') as decisions,
                       count(*) filter (where state = 'resolved') as resolved,
                       sum(cost_usd) as cost
                from rupert.mention
                where observed_at >= (now() at time zone 'utc')::date
                                     - (%(days)s - 1)
                group by 1
            ) as counted on counted.on_day = day::date
            order by day
            """,
            {"days": days},
        )
        return [
            Day(day=r[0], decisions=int(r[1]), resolved=int(r[2]), cost_usd=Decimal(r[3]))
            for r in cur.fetchall()
        ]


def standings(
    conn: psycopg.Connection,
    *,
    days: int = LEADERBOARD_DAYS,
    limit: int = LEADERBOARD_SIZE,
    model: str,
) -> list[Standing]:
    """The securities being talked about, loudest first.

    Two queries rather than one: the leaderboard is chosen first, and only then
    are the readings for those securities fetched. The alternative pulls every
    reading in the window to throw most of them away.

    **The tone is `reduce.mood`'s**, called rather than reimplemented in SQL.
    That keeps one definition of what a night's tone is — the trimming, the
    minimum item count, the quantisation — so the number on this page and the
    number a pillar would score cannot drift. A security below the floor shows
    its mention count and no tone, which is the honest rendering of `None`.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select m.security_id, s.primary_symbol, s.name, count(*) as mentions,
                   count(r.id) as read
            from rupert.mention m
            join security s on s.id = m.security_id
            join social_item si on si.id = m.social_item_id
            left join rupert.reading r on r.mention_id = m.id and r.model = %(model)s
            where m.state = 'resolved'
              and si.created_utc >= now() - make_interval(days => %(days)s)
            group by 1, 2, 3
            order by mentions desc, s.primary_symbol
            limit %(limit)s
            """,
            {"days": days, "limit": limit, "model": model},
        )
        top = cur.fetchall()
    if not top:
        return []

    ids = [int(r[0]) for r in top]
    with conn.cursor() as cur:
        cur.execute(
            """
            select m.security_id, r.positive, r.negative, r.neutral
            from rupert.reading r
            join rupert.mention m on m.id = r.mention_id
            join social_item si on si.id = m.social_item_id
            where m.security_id = any(%(ids)s) and m.state = 'resolved'
              and r.model = %(model)s
              and si.created_utc >= now() - make_interval(days => %(days)s)
            """,
            {"ids": ids, "days": days, "model": model},
        )
        # The three probabilities travel, not `positive - negative`: `mood`
        # weights by how much of a view each reading carries, and the collapsed
        # scalar has thrown that away.
        tones: dict[int, list[ToneReading]] = {}
        for security_id, positive, negative, neutral in cur.fetchall():
            tones.setdefault(int(security_id), []).append(
                ToneReading(float(positive), float(negative), float(neutral))
            )

    out: list[Standing] = []
    for security_id, symbol, name, mentions, read in top:
        out.append(
            Standing(
                security_id=int(security_id),
                symbol=str(symbol),
                name=str(name),
                mentions=int(mentions),
                read=int(read),
                mood=mood(tones.get(int(security_id), [])),
                attention=_attention(conn, int(security_id)),
            )
        )
    return out


def _attention(conn: psycopg.Connection, security_id: int) -> Decimal | None:
    """Tonight against this security's own baseline, or None without one.

    Its own history, never the universe's: a security that is always discussed
    and one that has never been discussed before both look unremarkable against
    a market-wide average, and the second is the entire signal.
    """
    from screener.rupert.store import mentions_by_day

    today = datetime.now().date()
    series = mentions_by_day(conn, security_id, through=today, days=BASELINE_DAYS + 1)
    if not series:
        return None
    return attention(series[-1], series[:-1])


def scored(
    conn: psycopg.Connection,
    security_id: int,
    *,
    days: int = WINDOW_DAYS,
    rolling: int = ROLLING_DAYS,
    model: str,
) -> list[Scored]:
    """What one security scored, day by day, oldest first.

    One query for the readings and all the pooling in Python, deliberately: the
    trimming, the floor and the quantisation are `reduce.mood`'s and calling it
    is what stops this page and a pillar disagreeing about what a tone is. A
    window function that recomputed a trimmed mean in SQL would be a second
    definition of the same number.

    Keyed on the **item's** day rather than `observed_at` — this answers what was
    being said about a company, which is a question about the corpus, not about
    when the resolver got round to reading it.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select date_trunc('day', si.created_utc at time zone 'utc')::date,
                   r.positive, r.negative, r.neutral
            from rupert.mention m
            join social_item si on si.id = m.social_item_id
            left join rupert.reading r on r.mention_id = m.id and r.model = %(model)s
            where m.security_id = %(security)s and m.state = 'resolved'
              and si.created_utc >= (now() at time zone 'utc')::date
                                    - (%(days)s + %(rolling)s - 1)
            order by 1
            """,
            {"security": security_id, "days": days, "rolling": rolling, "model": model},
        )
        rows = cur.fetchall()

    per_day: dict[date, list[ToneReading]] = {}
    counted: dict[date, int] = {}
    for day, positive, negative, neutral in rows:
        counted[day] = counted.get(day, 0) + 1
        if positive is not None and negative is not None and neutral is not None:
            per_day.setdefault(day, []).append(
                ToneReading(float(positive), float(negative), float(neutral))
            )

    today = datetime.now(UTC).date()
    out: list[Scored] = []
    for back in range(days - 1, -1, -1):
        day = today - timedelta(days=back)
        window = [day - timedelta(days=n) for n in range(rolling)]
        pooled = [tone for d in window for tone in per_day.get(d, ())]
        out.append(
            Scored(
                day=day,
                mentions=counted.get(day, 0),
                window_mentions=sum(counted.get(d, 0) for d in window),
                # `reduce.mood` decides whether there is enough to say anything,
                # and returns None when there is not. That judgement is not
                # repeated here.
                tone=(found.tone if (found := mood(pooled)) else None),
            )
        )
    return out


def review_total(conn: psycopg.Connection, *, state: str | None = None) -> int:
    """How many decisions the review list can page through.

    Counted rather than derived from `counts()`, because that one is keyed on
    state across the whole table and this has to agree with the page beneath it
    — a filter and a total computed two different ways is how a pager ends up
    offering a page that is empty when you reach it.
    """
    with conn.cursor() as cur:
        if state:
            cur.execute(
                "select count(*) from rupert.mention where state = %s", [state]
            )
        else:
            cur.execute("select count(*) from rupert.mention")
        row = cur.fetchone()
    return int(row[0]) if row else 0


def review(
    conn: psycopg.Connection,
    *,
    limit: int = REVIEW_SIZE,
    offset: int = 0,
    state: str | None = None,
) -> list[Decided]:
    """The most recent decisions, with the sentences they were made about.

    **The review surface, and the reason this page exists at all.** A wrong link
    is the failure mode of the whole layer and no aggregate will show you one;
    the only way to catch "ALL" being read as Allstate in "I put it ALL on
    calls" is to read the sentence beside the decision. So the refusals are
    here too, on the terms `magpie.attempt` set: a thing refused and a thing
    never tried are different facts and both are worth seeing.
    """
    clause = "where m.state = %(state)s" if state else ""
    with conn.cursor() as cur:
        cur.execute(
            # Composed only from a module-owned constant, never from the caller:
            # `state` arrives as a bound parameter and this branch only decides
            # whether the clause is present at all.
            """
            select m.id, m.state, m.chosen, s.primary_symbol, m.confidence,
                   m.candidates, m.claim_kind, m.injection, m.position_talk,
                   r.positive, r.negative,
                   si.subreddit,
                   left(coalesce(nullif(si.title, '') || ' ', '') || si.body, 240),
                   si.created_utc, m.observed_at
            from rupert.mention m
            join social_item si on si.id = m.social_item_id
            left join security s on s.id = m.security_id
            left join rupert.reading r on r.mention_id = m.id
            """
            + clause
            + """
            order by m.observed_at desc, m.id desc
            limit %(limit)s offset %(offset)s
            """,
            {"limit": limit, "offset": offset, "state": state},
        )
        rows = cur.fetchall()

    out: list[Decided] = []
    for row in rows:
        (
            mention_id, state_, chosen, symbol, confidence, candidates, claim_kind,
            injection, position_talk, positive, negative, subreddit, excerpt, at, observed,
        ) = row
        out.append(
            Decided(
                id=int(mention_id),
                state=str(state_),
                chosen=str(chosen) if chosen else None,
                symbol=str(symbol) if symbol else None,
                confidence=Decimal(confidence) if confidence is not None else None,
                candidates=tuple(str(c) for c in (candidates or ())),
                claim_kind=str(claim_kind) if claim_kind else None,
                injection=Decimal(injection) if injection is not None else None,
                position_talk=Decimal(position_talk) if position_talk is not None else None,
                # Derived here for the same reason `Sentiment.score` is derived
                # in its client: one definition, computed where the inputs are.
                tone=(
                    Decimal(positive) - Decimal(negative)
                    if positive is not None and negative is not None
                    else None
                ),
                subreddit=str(subreddit) if subreddit else None,
                excerpt=" ".join(str(excerpt or "").split()),
                at=at,
                observed_at=observed,
            )
        )
    return out


def for_narrative(
    conn: psycopg.Connection, security_id: int, *, days: int, limit: int
) -> list[tuple[str, str | None]]:
    """The sentences a narrative reads: excerpt and claim kind, newest first.

    **No tone and no confidence**, deliberately, and this is the query that
    enforces it rather than the prompt: a model shown a number will repeat it
    back as though it had found it, and the whole line between narrative
    extraction and scoring is that the model never sees one.

    Resolved mentions only. A `none` is by definition not about this security.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select left(coalesce(nullif(si.title, '') || ' ', '') || si.body, 240),
                   m.claim_kind
            from rupert.mention m
            join social_item si on si.id = m.social_item_id
            where m.security_id = %(security)s and m.state = 'resolved'
              and si.created_utc >= now() - make_interval(days => %(days)s)
            order by si.created_utc desc
            limit %(limit)s
            """,
            {"security": security_id, "days": days, "limit": limit},
        )
        return [(str(r[0] or ""), str(r[1]) if r[1] else None) for r in cur.fetchall()]


def read_narrative(
    conn: psycopg.Connection, security_id: int, *, as_of: date, model: str
) -> tuple[str, int, int] | None:
    """Today's narrative for this security, or None if it has not been written.

    Keyed on the day rather than cached for a duration: two people opening the
    same security an hour apart are asking the same question of the same corpus
    and should not pay twice for the same paragraph.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select text, mentions_used, window_days
            from rupert.narrative
            where security_id = %s and as_of = %s and model = %s
            """,
            [security_id, as_of, model],
        )
        row = cur.fetchone()
    return (str(row[0]), int(row[1]), int(row[2])) if row else None


def save_narrative(
    conn: psycopg.Connection,
    security_id: int,
    *,
    as_of: date,
    text: str,
    mentions_used: int,
    window_days: int,
    model: str,
    cost_usd: float,
) -> None:
    """Keep one narrative. `do nothing` on a race, not `do update`.

    Two tabs opened at once would otherwise each pay and the second would
    overwrite the first with a paraphrase of it. Whichever arrived first is as
    good an answer as the other.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into rupert.narrative (
                security_id, as_of, text, mentions_used, window_days, model, cost_usd
            ) values (%s, %s, %s, %s, %s, %s, %s)
            on conflict (security_id, as_of, model) do nothing
            """,
            [security_id, as_of, text, mentions_used, window_days, model, cost_usd],
        )


def narratives_today(conn: psycopg.Connection, *, as_of: date) -> int:
    """How many narratives have been written today, across everybody.

    Counted from `rupert.narrative` rather than from the audit trail, because
    this is the thing being capped and a cap should count the rows it limits —
    an audit query that missed one would read as room that is not there.
    """
    with conn.cursor() as cur:
        cur.execute(
            "select count(*) from rupert.narrative where as_of = %s", [as_of]
        )
        row = cur.fetchone()
    return int(row[0]) if row else 0


def security_named(
    conn: psycopg.Connection, security_id: int
) -> tuple[str, str] | None:
    """`(symbol, name)` for one security, or None if it is not one."""
    with conn.cursor() as cur:
        cur.execute(
            "select primary_symbol, name from security where id = %s", [security_id]
        )
        row = cur.fetchone()
    return (str(row[0]), str(row[1])) if row else None


def frontiers(conn: psycopg.Connection) -> list[Frontier]:
    """How far through each corpus the resolver has read."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select corpus, read_through, items_read, updated_at
            from rupert.progress order by corpus
            """
        )
        return [
            Frontier(
                corpus=str(r[0]), read_through=r[1], items_read=int(r[2]), updated_at=r[3]
            )
            for r in cur.fetchall()
        ]


def passes(conn: psycopg.Connection, source: int, *, limit: int = 10) -> list[Pass]:
    """The last few recorded passes, newest first.

    From `ingest_run`, which is where every other ingest in this project keeps
    the same thing — so "when did it last run and did it finish" is one query
    against one table for all of them rather than a per-subsystem invention.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select endpoint, status, started_at, finished_at,
                   securities_requested, securities_ok, error
            from ingest_run
            where source_id = %s
            order by started_at desc, id desc
            limit %s
            """,
            [source, limit],
        )
        return [
            Pass(
                endpoint=str(r[0]),
                status=str(r[1]),
                started_at=r[2],
                finished_at=r[3],
                shortlisted=int(r[4]) if r[4] is not None else None,
                resolved=int(r[5]) if r[5] is not None else None,
                error=str(r[6]) if r[6] else None,
            )
            for r in cur.fetchall()
        ]


def covered(conn: psycopg.Connection, *, days: int = LEADERBOARD_DAYS) -> tuple[int, int]:
    """How many securities were mentioned, and how many enough to score.

    **The number this page exists to keep honest.** Measured over three days of
    the live corpus, 291 securities were mentioned and 57 reached ten mentions,
    out of 1,504 active — so a Sentiment pillar would be absent for most of the
    universe. That is a fact about the corpus rather than a fault, and it should
    be on the page rather than discovered when the pillar is wired.
    """
    from screener.rupert.reduce import MIN_MENTIONS

    with conn.cursor() as cur:
        cur.execute(
            """
            select count(*), count(*) filter (where n >= %(floor)s)
            from (
                select m.security_id, count(*) as n
                from rupert.mention m
                join social_item si on si.id = m.social_item_id
                where m.state = 'resolved'
                  and si.created_utc >= now() - make_interval(days => %(days)s)
                group by 1
            ) as per_security
            """,
            {"days": days, "floor": MIN_MENTIONS},
        )
        row = cur.fetchone()
    return (int(row[0]), int(row[1])) if row else (0, 0)


def active_securities(conn: psycopg.Connection) -> int:
    """The universe as it stands, so coverage has a denominator."""
    with conn.cursor() as cur:
        cur.execute("select count(*) from security where is_active")
        row = cur.fetchone()
    return int(row[0]) if row else 0
