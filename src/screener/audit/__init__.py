"""The audit trail: what the platform did, who asked, and what it cost.

Writing is `record()`, which never raises — losing a log line is a smaller
problem than an operation failing because Postgres blinked. Reading is `page()`,
`spend()` and `operations()`, which back the interface.

Its own `audit` schema rather than `public`, for the reason `auth` has one:
nothing here is a fact or a score, and the trail outlives the data it describes.
"""

from screener.audit.models import KINDS, ActorKind, Event, Kind, Outcome, Spend
from screener.audit.reader import PAGE_SIZE, operations, page, spend
from screener.audit.writer import record

__all__ = [
    "ActorKind",
    "Event",
    "KINDS",
    "Kind",
    "Outcome",
    "PAGE_SIZE",
    "Spend",
    "operations",
    "page",
    "record",
    "spend",
]
