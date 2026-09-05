"""The audit trail: what the platform did, who asked, and what it cost.

Writing is `record()`, which never raises — losing a log line is a smaller
problem than an operation failing because Postgres blinked. Reading is `page()`,
`spend()`, `by_actor()` and `operations()`, which back the interface.

Its own `audit` schema rather than `public`, for the reason `auth` has one:
nothing here is a fact or a score, and the trail outlives the data it describes.
"""

from screener.audit.models import (
    KINDS, ActorKind, ActorSpend, Event, Kind, Outcome, Spend,
)
from screener.audit.people import Person, avatar, fold
from screener.audit.reader import (
    HANDOFF_WINDOW_MINUTES, PAGE_SIZE, by_actor, last_handoff_context, operations,
    page, spend,
)
from screener.audit.writer import record

__all__ = [
    "ActorKind",
    "HANDOFF_WINDOW_MINUTES",
    "ActorSpend",
    "Event",
    "KINDS",
    "Kind",
    "Outcome",
    "PAGE_SIZE",
    "Person",
    "Spend",
    "avatar",
    "by_actor",
    "fold",
    "last_handoff_context",
    "operations",
    "page",
    "record",
    "spend",
]
