"""What the corpus was saying about one security, in English.

**Narrative extraction, never a score** — the rule `screener.ai` is built around
and the reason this is allowed to exist at all. The model reads the same
sentences a person reads on `/rupert` and says what they were about. It supplies
no number, is shown no number, and is told so in as many words: the tone printed
beside it on the page is FinBERT's, and a model handed a figure will launder it
back into prose that sounds like a finding.

Two halves again. `prompt` is pure — sentences in, two strings out — so what is
actually asked can be read and tested without a key, a network or a database.
`write` is the only thing here that spends money, and it goes through
`screener.ai.complete` like every other model call in the tree so the cost comes
back from OpenRouter rather than from a price table kept here.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from screener.ai import DEFAULT_MODEL, AiError, complete

logger = logging.getLogger(__name__)

# How many mentions one narrative reads. Bounded because this is the only place
# in Rupert that pays per *token* rather than per decision: forty comments at a
# 240-character excerpt is about 3,000 tokens, which is a fraction of a penny on
# the cheap model. Reading four hundred would cost ten times that to say the
# same thing, because the fortieth comment on a busy day is already a repeat.
MAX_MENTIONS = 40

# How far back it reads. The same seven days the leaderboard shows, so the
# paragraph explains the row it was opened from rather than some other window.
WINDOW_DAYS = 7

# Deliberately short. A narrative that runs to a page is one nobody reads, and
# the whole value here is being able to take in what a corpus said at a glance
# before deciding whether to go and read the sentences themselves.
MAX_TOKENS = 320

# How many narratives may be written in a day, across everybody.
#
# **Its own counter, on `screener.magpie`'s precedent**, and the unit cost is why.
# A narrative reads forty comments — roughly 3,000 input tokens on the cheap
# model, about $0.00026 — which is five times a chat reply. Sharing
# `DAILY_SPEND_CAP_USD` would mean a morning of clicking through the leaderboard
# quietly eating the allowance Steven answers from, and the first symptom would
# be the assistant going quiet for a reason nobody could connect to this page.
#
# Both caps apply: this one bounds how many are written, and `budget.check`
# still runs before each, so a person already over their personal cap cannot
# write one even while this counter has room.
#
# Generous against the shape of the thing — the leaderboard is twelve securities
# and each is cached for the day, so the honest ceiling on useful narratives is
# about twelve. Fifty allows for re-reading across days without being a number
# anybody hits by working normally.
DAILY_MAX = 50

SYSTEM = """You summarise what retail investors on Reddit were saying about one company.

You are given real comments, each with the kind of claim it makes. Write two or \
three short sentences describing what the discussion was about: the topics, the \
recurring arguments, and anything several people independently raised.

Rules, all of them absolute:
- Describe what was said. Never say whether it is true, and never say whether \
the stock is worth buying, selling or holding.
- Never invent a number. If the comments do not contain a figure, there is no \
figure. Do not estimate, rank, score or rate anything.
- Do not characterise overall sentiment as a value or a score. You may say what \
people were arguing about and that opinion was divided; you may not say it was \
"0.4 positive" or "bullish overall" as though measured.
- If the comments are mostly jokes, memes or position announcements rather than \
claims about the business, say exactly that. That is a useful answer.
- No preamble, no headings, no bullet points. Plain sentences.
- If there is too little here to characterise, say so in one sentence."""


@dataclass(frozen=True, slots=True)
class Mention:
    """One comment as the narrative reads it: the words and what kind they are.

    Deliberately carries no tone and no confidence. The model is not shown the
    numbers, so it cannot repeat one back as though it had found it — which is
    the failure that would make a paragraph look like a measurement.
    """

    excerpt: str
    claim_kind: str | None


@dataclass(frozen=True, slots=True)
class Written:
    """A narrative, and what it cost to write."""

    text: str
    mentions_used: int
    model: str
    cost_usd: float


def prompt(symbol: str, name: str, mentions: Sequence[Mention]) -> tuple[str, str]:
    """The system and user messages for one security. Pure.

    Kept here rather than inline at the call site for the reason
    `rupert.questions` keeps its questions: a prompt is the thing most likely to
    be edited in a hurry, and having it in one place makes a change to it show
    up as a diff worth reading.
    """
    lines = []
    for mention in mentions[:MAX_MENTIONS]:
        kind = f"[{mention.claim_kind}] " if mention.claim_kind else ""
        text = " ".join(mention.excerpt.split())
        if text:
            lines.append(f"- {kind}{text}")

    user = (
        f"Company: {name} ({symbol})\n"
        f"Comments from the last {WINDOW_DAYS} days, most recent first:\n\n"
        + "\n".join(lines)
    )
    return SYSTEM, user


def write(
    symbol: str,
    name: str,
    mentions: Sequence[Mention],
    *,
    model: str = DEFAULT_MODEL,
    transport=None,
) -> Written | None:
    """Ask the cheap model what the discussion was about. None if it could not.

    Returns None rather than raising, because a narrative is the one thing on
    this page that is decoration: the tone, the counts and the sentences are all
    still there without it, and a panel that failed to summarise should say so
    rather than take the page down with it.

    `transport` is the house seam, so a test exercises this without a key.
    """
    if not mentions:
        return None

    system, user = prompt(symbol, name, mentions)
    try:
        answer = complete(
            system=system,
            user=user,
            model=model,
            max_tokens=MAX_TOKENS,
            # Low, not zero. This is a summary of supplied text rather than a
            # generative task, and the variation a higher temperature buys is
            # variation in something that should read the same twice.
            temperature=0.2,
            transport=transport,
        )
    except AiError as exc:
        logger.warning("could not write a narrative for %s: %s", symbol, exc)
        return None

    text = answer.text.strip()
    if not text:
        return None
    if answer.truncated:
        # A `length` stop cuts mid-sentence. Kept rather than discarded — a
        # paragraph that stops early still says what the discussion was about —
        # but marked, so nobody reads the cut as the model trailing off.
        text = text.rstrip() + " …"

    return Written(
        text=text,
        mentions_used=min(len(mentions), MAX_MENTIONS),
        model=answer.model,
        cost_usd=answer.cost_usd,
    )
