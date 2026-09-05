"""The gateway client and its lifecycle."""

import logging

import asyncio

import discord
from discord import app_commands

from screener.bot import agent
from screener.bot.checks import NotPermitted
from screener.bot.commands import COMMANDS
from screener.bot.config import BotConfig
from screener.audit import record

logger = logging.getLogger(__name__)


class ScreenerBot(discord.Client):
    """A slash-command client and nothing more.

    Intents are built up from `none()` rather than trimmed down from
    `default()`. `default()` is everything except the three privileged intents,
    which means reactions, voice states, typing, invites and integrations all
    arrive to be decoded and thrown away. Three are switched on and no more.

    `message_content` stays **off**, and the mention handler still works:
    Discord sends content regardless for messages that mention the app, for
    DMs, and for the app's own messages. Mentioning the bot is the only way to
    talk to it, so the one thing it needs to read is the one thing it is given
    without asking for blanket access to every channel it can see.
    """

    def __init__(self, config: BotConfig) -> None:
        intents = discord.Intents.none()
        intents.guilds = True          # channel objects, so replies can be sent
        intents.guild_messages = True  # MESSAGE_CREATE in the server
        intents.dm_messages = True     # and a direct message works too
        super().__init__(intents=intents)
        self._config = config
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        for command in COMMANDS:
            self.tree.add_command(command)
        self.tree.on_error = self._on_command_error

        if self._config.guild_id is None:
            # Global commands propagate for about an hour, which reads as "the
            # bot is broken" to anyone testing immediately after a deploy.
            logger.warning(
                "DISCORD_GUILD_ID is not set; syncing globally, which can take "
                "up to an hour to appear"
            )
            synced = await self.tree.sync()
        else:
            guild = discord.Object(id=self._config.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)

        # A wrong guild id fails silently: the commands land in a server nobody
        # is looking at and /ping simply never appears. Naming the guild and the
        # count here is what makes that diagnosable from the log.
        logger.info(
            "synced %d command(s) to guild %s: %s",
            len(synced),
            self._config.guild_id or "global",
            ", ".join(sorted(c.name for c in synced)) or "none",
        )

    async def on_ready(self) -> None:
        # The build and model live here rather than on the end of every reply:
        # always visible, said once, and it costs nothing to keep current.
        await self.change_presence(
            activity=discord.CustomActivity(name=agent.presence())
        )
        who = f"{self.user} ({self.user.id})" if self.user else "unknown"
        logger.info(
            "gateway connected as %s, permitted users: %s",
            who,
            ", ".join(str(i) for i in sorted(self._config.allowed_user_ids)) or "none",
        )

    async def on_message(self, message: discord.Message) -> None:
        """Answer when mentioned. The first agent-style feature.

        Deliberately mention-only. A bot that replies to everything is noise,
        and reading every message would need the privileged intent this
        avoids.
        """
        # Ignore itself and every other bot, first and unconditionally. A bot
        # that answers a bot is a loop that costs money on every lap.
        if message.author.bot or self.user is None:
            return
        if self.user not in message.mentions:
            return

        if not self._config.permits(message.author.id):
            logger.warning("refused a mention from discord user %s", message.author.id)
            await asyncio.to_thread(
                record,
                kind="agent",
                operation=agent.OPERATION,
                actor=str(message.author.id),
                actor_kind="discord",
                outcome="refused",
            )
            return

        # Strip the mention itself so the model sees the question rather than
        # a raw <@id> token.
        question = message.content.replace(f"<@{self.user.id}>", "").strip()

        async with message.channel.typing():
            reply = await agent.answer(
                question, actor=str(message.author.id), actor_kind="discord"
            )

        await message.reply(reply, mention_author=False)

    async def _on_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        """One place every command's failure is answered.

        Refusals and errors are both ephemeral: an unauthorised attempt should
        not be broadcast to the channel, and a traceback is nobody's business
        but the log's.
        """
        if isinstance(error, NotPermitted):
            message = "Not permitted."
        else:
            logger.exception("command failed", exc_info=error)
            message = "That failed. The log has the detail."

        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            # An interaction token dies three seconds after arrival if it was
            # never answered. Past that there is nobody left to apologise to,
            # and the log line above is the whole record.
            logger.warning("could not report the failure to the caller")
