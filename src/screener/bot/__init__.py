"""A Discord bot, as a command surface rather than a notifier.

Outbound alerts are not this. They stay a single webhook POST in
`screener.notify`, which is the part of the design that was never in question.
This is the other direction: a gateway connection that takes slash commands,
and the place agent-style features will be built.

Runs as its own process — `python -m screener.bot` — so the event loop it needs
stays out of everything else. Nothing in `screener.fetch`, `screener.notify` or
`screener.health` becomes async because of it.

Only the config is re-exported. `COMMANDS` and the client deliberately are not:
importing them pulls in `discord.py` and an event loop, and the self-test and
the test suite want to ask whether the bot is configured without paying for
either. Reach into `screener.bot.commands` if you actually need the commands.
"""

from screener.bot.config import BotConfig

__all__ = ["BotConfig"]
