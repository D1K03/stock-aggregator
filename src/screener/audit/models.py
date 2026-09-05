"""What an audit row looks like on the way in and on the way out."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

Kind = Literal["agent", "command", "tool", "system"]
ActorKind = Literal["discord", "github", "system"]
Outcome = Literal["ok", "refused", "error"]

KINDS: tuple[Kind, ...] = ("agent", "command", "tool", "system")


@dataclass(frozen=True, slots=True)
class Event:
    """One thing the platform did, as it is read back."""

    id: int
    occurred_at: datetime
    kind: str
    operation: str
    actor: str
    actor_kind: str
    outcome: str
    model: str | None
    prompt_tokens: int
    completion_tokens: int
    cost_usd: Decimal
    duration_ms: int | None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True, slots=True)
class Spend:
    """The totals shown above the table.

    Both a lifetime figure and a recent one, because a number that only ever
    grows tells you nothing about whether something changed today.
    """

    events: int
    total_cost: Decimal
    total_tokens: int
    events_24h: int
    cost_24h: Decimal
    tokens_24h: int


@dataclass(frozen=True, slots=True)
class ActorSpend:
    """What one person cost.

    Two people share this deployment and one bill. A single total says the
    month was cheap or expensive; it does not say whose questions did it, which
    is the thing either of them would actually want to know.

    Only paid events count: a tool call bills nothing, and folding those in
    would rank whoever pressed the most buttons rather than whoever spent the
    most money.
    """

    actor: str
    actor_kind: str
    events: int
    cost: Decimal
    tokens: int
    cost_24h: Decimal
    last_seen: datetime
