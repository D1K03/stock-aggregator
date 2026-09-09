"""What the two halves of magpie pass each other.

Frozen, and deliberately small. `Extracted` is what came out of an HTML string
and knows nothing about a database; `Document` is what was stored and knows
nothing about how it was fetched. The one field bridging them is `strategy`,
because a document that cost money should say so where it is read rather than
only in the audit trail.
"""

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True, slots=True)
class Extracted:
    """An article, out of the HTML and before anything is stored.

    `canonical_url` is whatever the page says it is, which is frequently not the
    URL that was asked for: a syndicated copy, a tracking parameter, an AMP
    variant. It is kept beside the requested URL rather than replacing it, so
    "what did we ask for" and "what does the publisher call this" stay separate
    questions.
    """

    title: str
    text: str
    author: str | None = None
    published: date | None = None
    language: str | None = None
    canonical_url: str | None = None

    @property
    def word_count(self) -> int:
        return len(self.text.split())


@dataclass(frozen=True, slots=True)
class Document:
    """A stored article.

    `strategy` names the rung that served it and `attempts` the ones tried
    first, so a page that had to be paid for is legible without joining to the
    audit trail.
    """

    id: int
    url: str
    host: str
    title: str
    text: str
    word_count: int
    strategy: str
    fetched_at: datetime
    #: Every rung tried, in order, ending in the one that answered. Carried out
    #: so an interface can show the climb rather than only the summit.
    attempts: tuple[str, ...] = ()
    cost_usd: float = 0.0
    author: str | None = None
    published: date | None = None
    canonical_url: str | None = None
    blob_path: str = ""
    #: True when this fetch changed the stored text. A re-scrape that found the
    #: same article writes nothing and reports False.
    stored: bool = False


@dataclass(frozen=True, slots=True)
class Refused:
    """Why a URL was not fetched.

    A refusal is not a failure and is not retried by escalating: `robots` means
    the site said no, and D4 is that escalating past that would invert the rule
    this exists to keep.
    """

    url: str
    reason: str
    detail: str = ""
    #: How far up the ladder it got before stopping. Empty for a refusal that
    #: happened before any request — robots, or an address that is a file.
    attempts: tuple[str, ...] = ()
