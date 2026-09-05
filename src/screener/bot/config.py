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
    # Which Discord account belongs to which signed-in web user, so a
    # conversation started in the dashboard can be picked up in Discord.
    # `github-login:discord-id` pairs, comma separated.
    #
    # Read from the environment like everything else, which means Infisical in
    # production. These are personal account identifiers and do not belong in
    # the repository, even though they are not secrets in the usual sense.
    user_map: dict[str, int] = field(default_factory=dict)

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

        pairs: dict[str, int] = {}
        for entry in (env.optional("DISCORD_USER_MAP") or "").split(","):
            entry = entry.strip()
            if not entry:
                continue
            login, _, discord_id = entry.partition(":")
            if not login or not discord_id.strip().isdigit():
                raise RuntimeError(
                    f"DISCORD_USER_MAP entry {entry!r} is not login:discord_id"
                )
            # Folded, because GitHub logins are case-insensitive and the
            # session carries whatever casing the owner chose.
            pairs[login.strip().lower()] = int(discord_id.strip())

        guild = env.optional("DISCORD_GUILD_ID")
        return cls(
            user_map=pairs,
            token=env.optional("DISCORD_BOT_TOKEN"),
            guild_id=int(guild) if guild and guild.isdigit() else None,
            allowed_user_ids=frozenset(ids),
        )

    @property
    def enabled(self) -> bool:
        """Whether the gateway process should start at all."""
        return self.token is not None

    def discord_id_for(self, login: str) -> int | None:
        """The Discord account belonging to a signed-in web user, if known."""
        return self.user_map.get(login.strip().lower())

    def permits(self, user_id: int) -> bool:
        """Whether this Discord account may run a command.

        An empty allow-list permits nobody. The other reading, that unset means
        everybody, turns a forgotten variable into a bot anyone in the server
        can drive.
        """
        return user_id in self.allowed_user_ids
