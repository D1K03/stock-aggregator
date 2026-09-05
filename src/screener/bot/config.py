"""Bot credentials and the allow-list, owned by the bot layer."""

from dataclasses import dataclass, field

from screener.config import env


@dataclass(frozen=True)
class BotConfig:
    """What the gateway process needs, and who may talk to it.

    Deliberately importable without `discord.py` loaded, so the self-test and
    the tests can ask whether the bot is configured without paying for the
    library or touching an event loop.
    """

    token: str | None = field(default=None, repr=False)
    guild_id: int | None = None
    allowed_user_ids: frozenset[int] = frozenset()

    @classmethod
    def from_env(cls) -> "BotConfig":
        raw = env.optional("ALLOWED_DISCORD_USER_IDS") or ""
        ids: set[int] = set()
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                ids.add(int(part))
            except ValueError as exc:
                # Loudly, because the quiet alternative is an allow-list one
                # entry shorter than its author believes, refusing someone for
                # a reason nothing reports.
                raise RuntimeError(
                    f"ALLOWED_DISCORD_USER_IDS contains {part!r}, which is not a "
                    "Discord user id"
                ) from exc

        guild = env.optional("DISCORD_GUILD_ID")
        return cls(
            token=env.optional("DISCORD_BOT_TOKEN"),
            guild_id=int(guild) if guild and guild.isdigit() else None,
            allowed_user_ids=frozenset(ids),
        )

    @property
    def enabled(self) -> bool:
        """Whether the gateway process should start at all."""
        return self.token is not None

    def permits(self, user_id: int) -> bool:
        """Whether this Discord account may run a command.

        An empty allow-list permits nobody. The other reading, that unset means
        everybody, turns a forgotten variable into a bot anyone in the server
        can drive.
        """
        return user_id in self.allowed_user_ids
