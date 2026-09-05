"""Steven: what the bot says when someone talks to it.

Mention him and he answers, calling tools when a tool answers better than a
guess. The work happens off the event loop and the answer is bounded by what
the system prompt lets him claim.

Everything here is written for token cost. A mention is cheap in isolation and
expensive in aggregate, and the levers that matter are: a short system prompt,
short tool descriptions, short tool results, a small reply budget, a hard cap
on tool rounds, and no conversation memory between mentions.
"""

import asyncio
import logging
from typing import Any

from screener.ai import AiError, converse
from screener.ai.models import SOLAR
from screener.bot.tools import dispatch, specs
from screener.config import env
from screener.provenance import git_sha

logger = logging.getLogger(__name__)

NAME = "Steven"

# Discord rejects a message over 2000 characters outright, which would turn a
# long answer into no answer.
MAX_REPLY = 2000

# Rounds of tool calling before Steven has to answer with what he has. Three is
# enough for "call a tool, read it, reply" with one retry, and every extra
# round re-sends the whole conversation.
MAX_TOOL_ROUNDS = 3

# Small on purpose. Discord messages are short, the prompt asks for brevity, and
# an unbounded budget is how a chatty model turns a one-line answer into a page.
MAX_REPLY_TOKENS = 400

# Solar Pro 4: cheaper than the DeepSeek default and a 524k context, which is
# the property that matters once a reply has to hold a transcript. Overridable,
# but `resolve_model` still applies, so a typo falls back to the default rather
# than billing against something nobody chose.
DEFAULT_AGENT_MODEL = SOLAR

# Terse deliberately. This is re-sent on every request and on every tool round,
# so a sentence saved here is saved several times per conversation.
SYSTEM_PROMPT = f"""You are {NAME}, assistant for a multi-signal equity screener, in its Discord server.

Rules:
1. No investment advice. No buy/sell/hold, no price targets, no entry points.
2. Never invent a number. Ingest is not built, so you have no scores, prices or market data. Say so plainly.
3. Say when you do not know.
4. Call a tool when one answers the question. Never guess what a tool would return.

Style. Warm, direct, and short. Two or three sentences unless asked for more. Answer first. No preamble, no restating the question, no bullet lists unless asked, no sign-off. Never end by offering a menu of things you could explain instead: if something is genuinely worth adding, just add it in a clause. Refuse in one sentence and move on.

Background you may explain if asked: percentiles are sector-relative; pillars are valuation, quality, momentum, sentiment, insider; alerts fire on a threshold crossing, not a state; every score traces to its raw inputs."""


def agent_model() -> str:
    return env.text("DISCORD_BOT_MODEL", DEFAULT_AGENT_MODEL)


def _truncate(text: str) -> str:
    if len(text) <= MAX_REPLY:
        return text
    # Cut at a word boundary and say it was cut: a reply that simply stops
    # reads as the bot breaking.
    cut = text[: MAX_REPLY - 20].rsplit(" ", 1)[0]
    return f"{cut} … (truncated)"


def _think(question: str) -> tuple[str, int, float]:
    """Run the tool loop and return (reply, tokens, cost).

    Synchronous, like every other call in this project. `answer` puts it on a
    worker thread.

    No history is carried between mentions. That is the single largest token
    saving available and it is also honest: Steven has no memory, and a
    conversation that silently accumulated context would get more expensive
    every message until someone noticed the bill.
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    tokens = 0
    cost = 0.0

    for round_number in range(MAX_TOOL_ROUNDS):
        completion = converse(
            messages=messages,
            model=agent_model(),
            tools=specs(),
            max_tokens=MAX_REPLY_TOKENS,
        )
        tokens += completion.prompt_tokens + completion.completion_tokens
        cost += completion.cost_usd

        if not completion.tool_calls:
            return completion.text.strip(), tokens, cost

        # The assistant turn goes back verbatim: a rebuilt one loses fields the
        # provider expects and the follow-up is rejected for a reason that
        # reads as nonsense.
        messages.append(completion.raw_message)
        for call in completion.tool_calls:
            logger.info("round %d: %s(%s)", round_number + 1, call.name, call.arguments)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": dispatch(call.name, call.arguments),
                }
            )

    # Out of rounds. Saying so is better than a silent stop, and better than
    # another paid round that might do the same thing again.
    return "I could not finish working that out.", tokens, cost


async def answer(question: str) -> str:
    """The reply to one message. Never raises.

    The model call is synchronous, so it runs on a worker thread. Calling it
    directly would block the gateway heartbeat for the length of the request
    and Discord would drop the connection as unresponsive.
    """
    if not question.strip():
        return (
            "I got a mention with no text. If this keeps happening, message "
            "content may not be reaching me."
        )

    try:
        reply, tokens, cost = await asyncio.to_thread(_think, question)
    except AiError as exc:
        logger.warning("agent reply failed: %s", exc)
        return "I could not reach the model just now."

    logger.info("replied on %s: %d tokens, $%.5f", agent_model(), tokens, cost)

    if not reply:
        return "The model returned nothing."
    return _truncate(reply)


def build_footer() -> str:
    """The provenance line, so an answer says what produced it."""
    return f"— {NAME} · {agent_model()} · build {git_sha()[:12]}"
