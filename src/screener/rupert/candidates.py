"""Finding the symbols a text might be about. Pure: no network, no database.

The deterministic half of the resolver, and the half that deliberately **refuses
to decide**. It produces a shortlist and stops; what a text is actually about is
`decide.py`'s question, because it needs the sentence and this only has the
token.

**Measured on the live corpus, 2026-09-20.** Over 22 days and 424,178 items,
`$TICKER` appears in 0.6% of comments -- so a cashtag-only shortlist reaches
almost none of the corpus and bare uppercase tokens have to be candidates too.
Doing that reaches 5.6% of items, and roughly a quarter of what it finds is
ordinary English: over three days the top matches included YOU, ON, IT, ARE,
ALL, AM, NOW and PM, alongside real traffic in MU, SNDK, AMD and NVDA.

**There is no blacklist, and that is the point of the whole layer.** Dropping
ALL, IT and ON to be rid of the false positives discards Allstate, Gartner and
ON Semiconductor permanently -- a word list is lossy in both directions, because
the difference between "I put it ALL on calls" and "ALL reported a combined
ratio of 91" is the sentence and not the word. So ambiguity is passed along
rather than resolved here, which is what `MAX_CANDIDATES` and the `none` option
in the question exist to absorb.
"""

import re
from collections.abc import Mapping

# A cashtag: unambiguous by construction, and the only form in which a
# single-character symbol is admissible. Bare `F` is the sixth most common word
# in r/wallstreetbets and it is not Ford.
CASHTAG = re.compile(r"\$([A-Za-z]{1,5})\b")

# A bare ticker, case-sensitively upper. Two characters is the floor because a
# single letter carries no signal without the `$`, and five is the ceiling
# because that is the longest symbol NASDAQ issues.
#
# Case matters and is doing real work: `all` is a word and `ALL` might be a
# company, and requiring the capitals is the one free filter that is not lossy.
BARE = re.compile(r"\b([A-Z]{2,5})\b")

# Above this the text is a list rather than a claim, and no single security is
# what it is about. Kept low deliberately: the question below it is "which one
# of these is this about", and a text offering twenty answers is telling us the
# question does not apply. It also bounds the request -- the model accepts 255
# options, which is not a limit this should ever be near.
MAX_CANDIDATES = 8


def candidates(text: str, lexicon: Mapping[str, str]) -> tuple[str, ...]:
    """Every symbol in `lexicon` this text might be about, in first-seen order.

    `lexicon` maps an upper-case current symbol to its company name and is
    passed in as data, which is what keeps this module free of a database
    connection -- the same split `screener.edgar.source.transactions` draws by
    taking the universe's CIKs as an argument.

    Order is the order of appearance rather than sorted, because the first
    symbol a comment names is usually its subject and the prompt reads better
    for it. Duplicates collapse: a comment saying NVDA six times is one
    candidate, not six.

    Returns empty for the 94.4% of the corpus that names nothing, and empty is
    the answer the caller acts on -- no request is made and nothing is stored.
    """
    if not text:
        return ()

    found: list[str] = []
    seen: set[str] = set()

    def offer(symbol: str) -> None:
        upper = symbol.upper()
        if upper in seen or upper not in lexicon:
            return
        seen.add(upper)
        found.append(upper)

    # Cashtags first, so that a text carrying both `$NVDA` and a bare `AMD`
    # leads with the one its author marked deliberately.
    for match in CASHTAG.finditer(text):
        offer(match.group(1))
    for match in BARE.finditer(text):
        offer(match.group(1))

    return tuple(found)


def crowded(found: tuple[str, ...]) -> bool:
    """Whether this shortlist is too long to be asking about one security."""
    return len(found) > MAX_CANDIDATES


def excerpt(text: str, limit: int) -> str:
    """The text as it should be sent, trimmed to `limit` characters.

    Trimmed on a word boundary where one is close, because the model's published
    guidance is that accuracy falls as the state fills with content unrelated to
    the decision -- and a sentence cut mid-word is content unrelated to the
    decision. A text with no space near the cut is truncated flat rather than
    searched backwards forever.
    """
    stripped = " ".join(text.split())
    if len(stripped) <= limit:
        return stripped
    cut = stripped[:limit]
    space = cut.rfind(" ")
    return cut[:space] if space > limit // 2 else cut
