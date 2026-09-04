"""Notification credentials, owned by the delivery layer."""

from dataclasses import dataclass, field

from screener.config import env


@dataclass(frozen=True)
class DiscordConfig:
    """Where Discord alerts go."""

    webhook_url: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls) -> "DiscordConfig":
        return cls(webhook_url=env.optional("DISCORD_WEBHOOK_URL"))

    @property
    def enabled(self) -> bool:
        return self.webhook_url is not None
