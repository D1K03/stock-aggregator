"""One pass over each corpus: shortlist, decide, then read what resolved.

The only module here that imports both halves, which is the shape
`screener.reddit.ingest` and `screener.edgar.ingest` already have.

Two phases rather than one loop, and the reason is the sentiment service. It
scores a batch under a semaphore of one and caps a request at 32 texts, so
asking it per item would be a round trip each and a queue of one-text batches.
Resolving first and reading second means tone is asked for in full batches, of
only the items that turned out to be about something.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import psycopg

from screener.audit import record
from screener.config import settings
from screener.rupert import candidates as shortlist
from screener.rupert import questions
from screener.rupert.config import RupertConfig
from screener.rupert.decide import DecideError, Decision, Throttled, decide
from screener.rupert.store import (
    DOCUMENT,
    SOCIAL,
    SOURCE_CODE,
    Text,
    advance,
    calls_today,
    last_pass,
    paused,
    spent_today,
    finish_run,
    lexicon,
    read_through,
    save_mention,
    save_reading,
    securities,
    source_id,
    start_run,
    unread_documents,
    unread_social,
)
from screener.sentiment import MAX_TEXTS, Sentiment, score

logger = logging.getLogger(__name__)

CORPORA = (SOCIAL, DOCUMENT)


@dataclass(frozen=True, slots=True)
class Report:
    """What one corpus's pass did. One per corpus, per pass."""

    corpus: str
    examined: int
    shortlisted: int
    resolved: int
    none: int
    unsure: int
    crowded: int
    failed: int
    read: int
    cost_usd: float
    failure: str | None = None


def once(
    config: RupertConfig | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    sleep=None,
    now: datetime | None = None,
) -> list[Report]:
    """Resolve and read the next slice of every corpus, once. Never raises."""
    config = config or RupertConfig.from_env()
    if not config.enabled:
        logger.info("RUPERT_DAILY_MAX_CALLS is 0; nothing to resolve")
        return []

    moment = now or datetime.now(UTC)
    reports: list[Report] = []
    with psycopg.connect(settings().database_url, autocommit=True) as conn:
        source = source_id(conn, SOURCE_CODE)
        # Checked at the top of the pass rather than at container start, so a
        # pause takes effect on the next wake instead of the next deploy. That
        # is the whole reason it is a row and not an environment variable.
        held, by, since = paused(conn)
        if held:
            logger.info(
                "rupert is paused%s; skipping this pass",
                f" (by {by})" if by else "",
            )
            return []
        names = lexicon(conn)
        ids = securities(conn)
        if not names:
            logger.warning("the universe holds no active symbols; nothing to resolve")
            return []
        for corpus in CORPORA:
            reports.append(
                _pass(
                    conn, source, corpus, config,
                    names=names, ids=ids, moment=moment,
                    transport=transport, sleep=sleep,
                )
            )

    spent = sum(r.cost_usd for r in reports)
    failed = [r.corpus for r in reports if r.failure]
    # One row for the whole pass, not one per decision. `record` opens a fresh
    # connection per call and the same table backs Steven's memory and the audit
    # page, so per-item rows would flood both -- the argument
    # `screener.reddit.ingest` already makes.
    #
    # `outcome` follows the data rather than the call, which is why a pass that
    # gave up on a corpus reads as partial even though nothing raised.
    record(
        kind="system",
        operation="rupert.resolve",
        outcome="partial" if failed else "ok",
        cost_usd=spent,
        detail={
            "examined": sum(r.examined for r in reports),
            "shortlisted": sum(r.shortlisted for r in reports),
            "resolved": sum(r.resolved for r in reports),
            "none": sum(r.none for r in reports),
            "unsure": sum(r.unsure for r in reports),
            "read": sum(r.read for r in reports),
            "failed": failed,
        },
    )
    logger.info(
        "rupert: %d examined, %d shortlisted, %d resolved, %d read, $%.5f",
        sum(r.examined for r in reports), sum(r.shortlisted for r in reports),
        sum(r.resolved for r in reports), sum(r.read for r in reports), spent,
    )
    return reports


def _pass(
    conn: psycopg.Connection,
    source: int,
    corpus: str,
    config: RupertConfig,
    *,
    names: dict[str, str],
    ids: dict[str, int],
    moment: datetime,
    transport: httpx.BaseTransport | None,
    sleep,
) -> Report:
    """One corpus. Records its own `ingest_run` either way."""
    frontier = read_through(conn, corpus)
    if frontier is None:
        # Never read. Start at the backfill horizon, which is *now* by default:
        # nothing already in the corpus is decided, and a deploy costs only what
        # arrives after it.
        frontier = moment - timedelta(days=config.backfill_days)
        # **Recorded immediately, before anything is read.** Without this the
        # frontier is only written by a pass that found something, so the
        # default — which by construction finds nothing on its first wake —
        # left `rupert.progress` empty and every later pass restarted at its own
        # "now". Everything that arrived between two passes fell down that gap,
        # silently, forever. Caught by the test for the no-backfill default.
        advance(conn, corpus, through=frontier, items=0)

    reader = unread_social if corpus == SOCIAL else unread_documents
    texts = reader(conn, after=frontier, limit=config.batch)
    if not texts:
        logger.info("%s: nothing new past %s", corpus, frontier.isoformat())
        return Report(corpus, 0, 0, 0, 0, 0, 0, 0, 0, 0.0)

    run_id = start_run(conn, source, f"{corpus}/resolve")
    budget = config.daily_max_calls - calls_today(conn, source)
    # What today already cost before this pass started, and what this pass has
    # cost so far. **Two variables, not one**: the ceiling is checked against
    # their sum, while the report carries only what this pass spent. Folding
    # them into one made the ceiling start from zero every pass, so it could
    # never fire — caught by the test that proves it bites.
    billed = spent_today(conn, source)
    counts = {"resolved": 0, "none": 0, "unsure": 0, "crowded": 0, "failed": 0}
    shortlisted = 0
    spent = 0.0
    failure: str | None = None
    # How far this pass actually got. The frontier only moves over items that
    # were decided, so a pass that ran out of budget half way leaves the rest
    # for the next one instead of skipping them for good.
    reached = frontier
    resolved: list[tuple[int, Text]] = []

    for text in texts:
        found = shortlist.candidates(text.content, names)
        if not found:
            # The 94.4% case. No row, no request -- `rupert.progress` is what
            # remembers this was looked at, which is the whole reason it exists.
            reached = text.at
            continue

        shortlisted += 1
        if shortlist.crowded(found):
            save_mention(conn, source, text, state="crowded", candidates=found)
            counts["crowded"] += 1
            reached = text.at
            continue

        # Two ceilings, checked together and both before the call rather than
        # after it — the point of a cap is that the request over it is never
        # paid for. The count is the working limit; the dollar figure is the one
        # that still holds if the price per call is not what we assumed.
        if budget <= 0:
            failure = f"daily call budget of {config.daily_max_calls} is spent"
            logger.info("%s: %s", corpus, failure)
            break
        if billed + spent >= config.daily_max_usd:
            failure = (
                f"daily spend ceiling of ${config.daily_max_usd:.2f} reached "
                f"(${billed + spent:.5f} billed today)"
            )
            logger.warning("%s: %s", corpus, failure)
            break

        try:
            answer = decide(
                state=questions.state(
                    shortlist.excerpt(text.content, config.max_chars), found, names
                ),
                questions=questions.build(found, names),
                transport=transport,
                sleep=sleep,
            )
        except Throttled as exc:
            # The one failure that ends the pass rather than the item. Every
            # further request extends a rate limit, which is the lesson
            # `screener.edgar` learned from SEC and the opposite of the call
            # `screener.reddit` makes about Arctic Shift's independent refusals.
            failure = str(exc)
            logger.warning("%s: %s", corpus, failure)
            break
        except DecideError as exc:
            save_mention(conn, source, text, state="failed", candidates=found)
            counts["failed"] += 1
            budget -= 1
            reached = text.at
            failure = failure or str(exc)
            logger.warning("%s item %d: %s", corpus, text.item_id, exc)
            continue

        budget -= 1
        spent += answer.cost_usd
        mention_id, state = _settle(
            conn, source, text, found, answer, config, ids=ids
        )
        counts[state] += 1
        if state == "resolved":
            resolved.append((mention_id, text))
        reached = text.at

    read = _read(conn, resolved, config)

    if reached > frontier:
        advance(conn, corpus, through=reached, items=len(texts))

    status = "partial" if failure else "ok"
    finish_run(
        conn, run_id, status,
        requested=shortlisted, ok=counts["resolved"], error=failure,
    )
    logger.info(
        "%s: %d examined, %d shortlisted, %d resolved, %d none, %d unsure, %d read",
        corpus, len(texts), shortlisted, counts["resolved"], counts["none"],
        counts["unsure"], read,
    )
    return Report(
        corpus=corpus,
        examined=len(texts),
        shortlisted=shortlisted,
        resolved=counts["resolved"],
        none=counts["none"],
        unsure=counts["unsure"],
        crowded=counts["crowded"],
        failed=counts["failed"],
        read=read,
        cost_usd=spent,
        failure=failure,
    )


def _settle(
    conn: psycopg.Connection,
    source: int,
    text: Text,
    found: Sequence[str],
    answer: Decision,
    config: RupertConfig,
    *,
    ids: dict[str, int],
) -> tuple[int, str]:
    """Turn one set of answers into a state, and write it. Returns (id, state).

    Four gates, in this order, and the order is the point: a text that is trying
    to steer the reader is refused before anything it says is believed, exactly
    as the published RAG cookbook checks injection before relevance.
    """
    choice = answer.choices.get(questions.WHICH)
    claim = answer.choices.get(questions.CLAIM_KIND)
    nouls = dict(answer.nouls)
    common = {
        "candidates": found,
        "choice": choice,
        "nouls": nouls,
        "claim": claim,
        "model": answer.model,
        "input_tokens": answer.input_tokens,
        "cost_usd": answer.cost_usd,
    }

    def write(state: str, security_id: int | None = None) -> tuple[int, str]:
        return save_mention(
            conn, source, text, state=state, security_id=security_id, **common
        ), state

    injection = nouls.get(questions.INJECTION)
    if injection is not None and injection > config.injection_ceiling:
        # Stored, not dropped. A text trying to steer a reader is a fact worth
        # keeping -- `magpie.attempt` records its refusals for the same reason,
        # so a thing that was refused and a thing nobody tried are different.
        logger.info("%s item %d looks like an injection", text.corpus, text.item_id)
        return write("none")

    if choice is None or choice.chosen == questions.NONE:
        return write("none")
    if choice.confidence < config.confidence_floor:
        # Below the gate, and kept with its whole distribution. This is the
        # state the threshold can be re-cut against later without re-deciding
        # anything, which is the only reason the `probabilities` column earns
        # its place.
        return write("unsure")

    security_id = ids.get(choice.chosen)
    if security_id is None:
        # The model returned an option that is not a current symbol. It was
        # offered only symbols and `none`, so this is the provider changing
        # something rather than a judgement -- and inventing a link from it
        # would be the half-done join migration 022 refused a column for.
        logger.warning("chosen symbol %r is not in the universe", choice.chosen)
        return write("failed")

    return write("resolved", security_id)


def _read(
    conn: psycopg.Connection,
    resolved: Sequence[tuple[int, Text]],
    config: RupertConfig,
) -> int:
    """Score the resolved mentions with FinBERT, in full batches.

    Tone is FinBERT's and only FinBERT's. The decision model is never asked how
    a text reads, which is what leaves `DESIGN.md`'s rule -- "an LLM never emits
    a score" -- standing unchanged rather than reversed.

    A failure here costs the readings and not the resolutions: `score` never
    raises and answers `None` for a whole batch it could not reach, and the
    mentions are already written. A mention with no reading is a visible state,
    not a silent zero.
    """
    written = 0
    for start in range(0, len(resolved), MAX_TEXTS):
        chunk = resolved[start : start + MAX_TEXTS]
        readings = score(
            [shortlist.excerpt(text.content, config.max_chars) for _, text in chunk]
        )
        if readings is None:
            logger.warning("sentiment service unreachable; %d mentions left unread", len(chunk))
            continue
        for (mention_id, _text), reading in zip(chunk, readings):
            if isinstance(reading, Sentiment):
                save_reading(conn, mention_id, reading, model=FINBERT)
                written += 1
    return written


# What the readings are stamped with. FinBERT is served from a pinned checkpoint
# converted to ONNX at build time, and the service does not report a version --
# so this names the thing rather than pretending to a revision it cannot see. A
# different checkpoint is a different string here and a second row there.
FINBERT = "prosusai/finbert"
