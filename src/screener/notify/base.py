"""The channel contract every delivery method implements."""

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

# Not a severity in the paging sense. It selects a colour and nothing else —
# this tool reports that a number crossed a line, and dressing that up as an
# incident would be the same overclaiming as calling it a buy signal.
Severity = Literal["info", "warning"]


class ChannelError(RuntimeError):
    """Delivery failed. The alert was not sent."""


@dataclass(frozen=True, slots=True)
class AlertField:
    """One labelled value in an alert — a pillar score, a delta, a flag."""

    name: str
    value: str
    inline: bool = True


@dataclass(frozen=True, slots=True)
class Alert:
    """A message to deliver, in a form no channel is specific to."""

    title: str
    body: str = ""
    fields: tuple[AlertField, ...] = field(default_factory=tuple)
    severity: Severity = "info"
    url: str | None = None


@runtime_checkable
class NotificationChannel(Protocol):
    """Anything that can deliver an `Alert`.

    `send` raises `ChannelError` on failure rather than returning a status,
    because a caller that forgets to check a boolean drops alerts silently and
    a muted channel is indistinguishable from a quiet market.
    """

    name: str

    def send(self, alert: Alert) -> None: ...
