"""Alert delivery.

One channel exists today — a Discord webhook — but callers depend on the
`NotificationChannel` protocol rather than on it. Adding Telegram, Signal,
Pushover, or a Discord gateway bot later means writing one class that satisfies
that protocol; nothing above it changes.

What an alert *says* is not decided here. Crossing detection, the pillar
breakdown, deduplication and the per-ticker cooldown belong to the alerting
layer, which does not exist yet.
"""

from screener.notify.base import Alert, AlertField, ChannelError, NotificationChannel
from screener.notify.config import DiscordConfig
from screener.notify.discord import DiscordWebhook

__all__ = [
    "Alert",
    "AlertField",
    "ChannelError",
    "DiscordConfig",
    "DiscordWebhook",
    "NotificationChannel",
]
