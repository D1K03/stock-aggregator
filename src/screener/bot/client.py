"""The gateway client and its lifecycle."""

import asyncio
import io
import logging
import time
from collections.abc import Sequence
from typing import Literal

import discord
import psycopg
from discord import app_commands

from screener import audit
from screener.audit import record
from screener.bot import agent, budget, render
from screener.bot.checks import NotPermitted
from screener.bot.commands import COMMANDS
from screener.bot.config import BotConfig
from screener.config import settings
from screener.transcribe import MAX_SECONDS, transcribe

logger = logging.getLogger(__name__)


def wants_reply(*, direct: bool, mentioned: bool) -> bool:
    """Whether a message is being addressed to the bot.

    One rule stated for two places: reply when you are being spoken to. In a
    shared channel that has to be explicit or the bot is noise in every
    conversation it can see. In a direct message there is nobody else it could
    be for, and making someone @-mention the only other participant in a
    two-person conversation reads as broken.

    A function rather than an inline condition because it is the whole
    behaviour of the change, and a `discord.Message` cannot be built in a test
    without a gateway.
    """
    return direct or mentioned


Hearing = Literal["nothing", "transcribe", "too_long"]

# What a voice message costs to answer, recorded as an operation of its own so
# `duration_ms` on it is where you would watch the CPU this feature spends.
TRANSCRIBE_OPERATION = "steven.transcribe"

# Discord's subtext marker. Small and grey, so the transcript reads as a receipt
# for what was heard rather than as part of what Steven said.
QUOTE = "-# "
MAX_QUOTE = 200


def hearing(durations: Sequence[float | None], *, cap: float = MAX_SECONDS) -> Hearing:
    """What to do with the voice messages attached to a Discord message.

    `durations` is `Attachment.duration` for each attachment that
    `is_voice_message()` claims is one. `None` should not reach here, since
    Discord sets duration and waveform together, and is treated as unknown
    rather than as zero.

    Three answers rather than a boolean, for the same reason `Budget` carries
    the numbers behind its own: "there is nothing to listen to" and "there is,
    and it is longer than we will pay to hear" want different replies. Collapsed
    into a boolean the second falls through to the empty-question guard, and
    Steven answers that message content may not be reaching him — which is
    wrong, and is the exact sentence that sends somebody off to check an intent.

    A function rather than an inline condition because it is the whole decision,
    and a `discord.Message` cannot be built in a test without a gateway.
    """
    if not durations:
        return "nothing"
    first = durations[0]
    if first is None or first <= 0 or first > cap:
        return "too_long"
    return "transcribe"


def with_transcript(transcript: str, answer: str, *, limit: int = agent.MAX_REPLY) -> str:
    """The answer, with what was heard above it.

    Discord rejects a message over 2000 characters outright, and `agent.respond`
    has already cut the answer to exactly that — so the quote cannot simply be
    prefixed on, or a long answer becomes no message at all, which is the one
    failure `MAX_REPLY` exists to prevent.

    The answer is what was asked for, so the quote gives way first: it is cut to
    `MAX_QUOTE`, and dropped entirely if even that would not fit. Newlines
    collapse to spaces because the subtext marker only styles its own line.
    """
    heard = " ".join(transcript.split())
    if not heard:
        return answer
    if len(heard) > MAX_QUOTE:
        heard = heard[: MAX_QUOTE - 1].rstrip() + "\u2026"
    line = f'{QUOTE}Heard: \u201c{heard}\u201d\n'
    if len(line) + len(answer) > limit:
        return answer
    return line + answer


class ScreenerBot(discord.Client):
    """A slash-command client and nothing more.

    Intents are built up from `none()` rather than trimmed down from
    `default()`. `default()` is everything except the three privileged intents,
    which means reactions, voice states, typing, invites and integrations all
    arrive to be decoded and thrown away. Three are switched on and no more.

    `message_content` stays **off**, and the message handler still works:
    Discord sends content regardless for messages that mention the app, for
    DMs, and for the app's own messages. Those are exactly the two ways to talk
    to this bot, so the one thing it needs to read is the one thing it is given
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
        """Answer a mention in the server, or anything at all in a DM.

        A mention is required in a channel and not in a DM, because those are
        the same rule stated twice: reply when you are being spoken to. In a
        shared channel that needs saying explicitly or the bot is noise; in a
        direct message there is nobody else it could be for, and making someone
        @-mention the only other participant in a two-person conversation is
        the kind of thing that reads as broken.
        """
        # Ignore itself and every other bot, first and unconditionally. A bot
        # that answers a bot is a loop that costs money on every lap.
        if message.author.bot or self.user is None:
            return

        direct = isinstance(message.channel, discord.DMChannel)
        if not wants_reply(direct=direct, mentioned=self.user in message.mentions):
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

        voices = [a for a in message.attachments if a.is_voice_message()]
        if hearing([a.duration for a in voices]) == "too_long":
            # Declined before the download, not after. The cap is about what
            # this box will spend a core on, and it has already been spent by
            # the time the bytes are here. No budget check either: refusing on
            # length costs nothing, and a database round trip to say so would
            # be silly.
            await message.reply(
                f"That is over {int(MAX_SECONDS)} seconds, which is longer than "
                "I will sit and listen to. Send a shorter one, or type it.",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        # Strip the mention itself so the model sees the question rather than
        # a raw <@id> token. A DM has none, and this is a no-op there.
        typed = message.content.replace(f"<@{self.user.id}>", "").strip()

        transcript, allowance = "", None
        if voices:
            transcript, allowance, refusal = await self._listen(message, voices[0])
            if refusal is not None:
                await message.reply(
                    refusal,
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

        # A caption alongside a voice note is still part of the question.
        question = " ".join(part for part in (typed, transcript) if part)

        # Only in a DM, and only shortly after one: the handoff message says
        # "ask me here and I will pick it up", and this is what picking it up
        # means. Without it the first follow-up has no antecedent and "can you
        # chart it" is answered with "which ticker?".
        context = (
            await asyncio.to_thread(self._handoff_context, message.author.id)
            if direct
            else ""
        )

        async with message.channel.typing():
            reply = await agent.respond(
                question,
                actor=str(message.author.id),
                actor_kind="discord",
                surface="discord",
                context=context,
                # Discord cannot draw, but it can display what the web service
                # drew, so the chart tool is allowed to produce one.
                can_draw=True,
                voice=bool(transcript),
                # Already checked, before a core was spent on transcription.
                # Passing it on rather than asking again keeps one turn to one
                # sum, and keeps the refusal wording where it lives.
                allowance=allowance,
            )
            # Rasterising is a HTTP call and a few milliseconds of work in
            # another container, so it goes on a worker thread like everything
            # else that would otherwise sit on the gateway heartbeat.
            images = await asyncio.to_thread(render.chart_pngs, reply.charts)

        files = [
            discord.File(io.BytesIO(png), filename=name) for name, png in images
        ]
        await message.reply(
            with_transcript(transcript, reply.text) if transcript else reply.text,
            files=files,
            mention_author=False,
            # `mention_author=False` suppresses the reply ping and nothing else.
            # Text that arrived as speech should not be able to ping a channel:
            # Whisper will not emit an <@id>, but "it cannot happen" is not a
            # reason to let it.
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _listen(
        self, message: discord.Message, voice: discord.Attachment
    ) -> tuple[str, "budget.Budget | None", str | None]:
        """Transcribe a voice message, or say why not.

        Returns the transcript, the allowance already spent checking, and a
        sentence to send instead when there is nothing to answer. The budget is
        checked before a byte is downloaded: transcription is a cost even though
        it is not a charge, and the point of a cap is that the request over it is
        never paid for.

        A refused allowance is not a refusal here — it is handed back so
        `agent.respond` produces the sentence and the audit row, which is where
        both already live.
        """
        actor = str(message.author.id)
        allowance = await asyncio.to_thread(budget.check, actor, "discord")
        if not allowance.allowed:
            return "", allowance, None

        started = time.perf_counter()
        async with message.channel.typing():
            # A coroutine over discord.py's own session, so it is awaited
            # rather than put on a worker thread. The transcription behind it
            # is synchronous httpx, and is.
            audio = await voice.read()
            spoken = await asyncio.to_thread(
                transcribe, audio, content_type=voice.content_type or ""
            )
        if spoken is None:
            return "", allowance, "I could not turn that into text just now."
        if not spoken.text:
            return "", allowance, "That came through empty. Nothing I could make out."

        await asyncio.to_thread(
            record,
            kind="agent",
            operation=TRANSCRIBE_OPERATION,
            actor=actor,
            actor_kind="discord",
            duration_ms=int((time.perf_counter() - started) * 1000),
            # Zero, and not because a core for twenty seconds is free. It is not
            # a charge, and this column is what was billed; an invented number
            # here would be a fiction in the one place that must not hold one.
            cost_usd=0,
            detail={
                "surface": "discord",
                "seconds": spoken.seconds,
                "bytes": len(audio),
                # A length, not the words. The question does reach the trail
                # on the reply row below, because that is where Steven's
                # memory lives and a spoken question is a question — but it
                # gets there once, as text, rather than twice.
                "chars": len(spoken.text),
            },
        )
        return spoken.text, allowance, None

    @staticmethod
    def _handoff_context(discord_user_id: int) -> str:
        """What they were looking at when they asked to continue here.

        Never raises. A missing antecedent costs one clarifying question; an
        exception here would cost the reply.
        """
        try:
            with psycopg.connect(
                settings().database_url, connect_timeout=3
            ) as conn:
                return audit.last_handoff_context(conn, str(discord_user_id))
        except Exception as exc:
            logger.warning("could not read the handoff context: %s", exc)
            return ""

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
