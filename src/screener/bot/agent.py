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
import time
from dataclasses import dataclass, field
from typing import Any

from screener.ai import AiError, converse
from screener.ai.models import SOLAR
from screener.bot import budget
from screener.bot.tools import Chart, collecting, dispatch, specs
from screener.audit import record
from screener.config import env
from screener.provenance import git_sha

logger = logging.getLogger(__name__)

NAME = "Steven"

# The audit operation these replies are filed under.
OPERATION = "steven.reply"

# Discord rejects a message over 2000 characters outright, which would turn a
# long answer into no answer.
MAX_REPLY = 2000

# Rounds of tool calling before Steven has to answer with what he has. Every
# round re-sends the whole conversation, so this is a cost ceiling as much as a
# loop guard: six leaves room for a chain of calls that build on each other,
# while still stopping a model that has started asking for the same thing over
# and over.
MAX_TOOL_ROUNDS = 6

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
SYSTEM_PROMPT = f"""You are {NAME}, assistant for a multi-signal equity screener, in Discord and on its dashboard.

Rules:
1. No investment advice. No buy/sell/hold, no price targets, no entry points.
2. Never invent a number. Figures come from tools only; the chart tool's are illustrative sample data, not real market data — say so when you quote them. No live prices.
3. Say when you do not know.
4. Call a tool when one answers the question. Never guess what a tool would return.

Style. A colleague in chat, not a support desk. Casual, contractions fine, a sentence or two unless asked for more. Answer first: no preamble, no restating the question, no bullet lists unless asked, no sign-off, no hedging. Never close by listing things you could explain instead. Turn something down in one line and move on.

For a ticker's history, high, low, biggest surge or drop, or a crossing: call `chart` with that mark. Where it draws, the point is marked and dated for them, so answer in one sentence rather than listing figures.

Asked what you can do or have access to, name your tools and what they report. You have no others.

If asked: percentiles are sector-relative; pillars are valuation, quality, momentum, sentiment, insider; alerts fire on a threshold crossing, not a state; every score traces to its raw inputs."""


@dataclass(frozen=True, slots=True)
class ToolRun:
    """One tool the model reached for, and how long it took."""

    name: str
    ms: int


@dataclass(frozen=True, slots=True)
class Reply:
    """An answer, and what it took to produce.

    The tools are carried out to the caller so an interface can show what was
    consulted. Discord does not use them today; the dashboard does.

    `charts` is the same idea for things drawn rather than said: they never
    entered the conversation, so they cost nothing per round and arrive at the
    surface intact rather than as the model's description of them.
    """

    text: str
    tokens: int = 0
    cost_usd: float = 0.0
    tools: tuple[ToolRun, ...] = field(default_factory=tuple)
    charts: tuple[Chart, ...] = field(default_factory=tuple)


def agent_model() -> str:
    return env.text("DISCORD_BOT_MODEL", DEFAULT_AGENT_MODEL)


def _truncate(text: str) -> str:
    if len(text) <= MAX_REPLY:
        return text
    # Cut at a word boundary and say it was cut: a reply that simply stops
    # reads as the bot breaking.
    cut = text[: MAX_REPLY - 20].rsplit(" ", 1)[0]
    return f"{cut} … (truncated)"


def _think(question: str, context: str = "", can_draw: bool = False) -> Reply:
    """Run the tool loop and return (reply, tokens, cost).

    Synchronous, like every other call in this project. `answer` puts it on a
    worker thread.

    No history is carried between mentions. That is the single largest token
    saving available and it is also honest: Steven has no memory, and a
    conversation that silently accumulated context would get more expensive
    every message until someone noticed the bill.

    `can_draw` says whether the caller can render a chart. Discord cannot, and
    a tool that announced one anyway would have Steven point at something that
    is not there.
    """
    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if context:
        # A second system turn rather than folded into the question, so the
        # model can tell what the person typed from what their screen happens
        # to show, and so "explain this" has an antecedent.
        messages.append(
            {
                "role": "system",
                "content": (
                    f"The person is currently looking at {context} "
                    "Answer about that when they say 'this' or ask what they "
                    "are looking at. Do not repeat it back unprompted."
                ),
            }
        )
    messages.append({"role": "user", "content": question})
    tokens = 0
    cost = 0.0

    used_tools: list[ToolRun] = []

    # One code path either way: with `can_draw` false the context collects
    # nothing and the tool sees that, so no chart is drawn and none is claimed.
    with collecting(can_draw) as drawn:
        return _rounds(messages, used_tools, drawn, tokens, cost)


def _rounds(
    messages: list[dict[str, Any]],
    used_tools: list[ToolRun],
    drawn: list[Chart],
    tokens: int,
    cost: float,
) -> Reply:
    """The tool-calling loop itself. Split out so the collecting context wraps it."""
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
            return Reply(
                text=completion.text.strip(),
                tokens=tokens,
                cost_usd=cost,
                tools=tuple(used_tools),
                charts=tuple(drawn),
            )

        # The assistant turn goes back verbatim: a rebuilt one loses fields the
        # provider expects and the follow-up is rejected for a reason that
        # reads as nonsense.
        messages.append(completion.raw_message)
        for call in completion.tool_calls:
            logger.info("round %d: %s(%s)", round_number + 1, call.name, call.arguments)
            started = time.perf_counter()
            content = dispatch(call.name, call.arguments)
            used_tools.append(
                ToolRun(call.name, int((time.perf_counter() - started) * 1000))
            )
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": content}
            )

    # Out of rounds. Saying so is better than a silent stop, and better than
    # another paid round that might do the same thing again.
    return Reply(
        text="I could not finish working that out.",
        tokens=tokens,
        cost_usd=cost,
        tools=tuple(used_tools),
        charts=tuple(drawn),
    )


async def answer(
    question: str, *, actor: str = "system", actor_kind: str = "discord"
) -> str:
    """The reply text alone. What Discord posts."""
    # Discord renders text, so nothing is drawn for it.
    return (
        await respond(question, actor=actor, actor_kind=actor_kind, surface="discord")
    ).text


async def respond(
    question: str,
    *,
    actor: str = "system",
    actor_kind: str = "system",
    surface: str = "discord",
    context: str = "",
    can_draw: bool = False,
) -> Reply:
    """The reply to one message. Never raises.

    The model call is synchronous, so it runs on a worker thread. Calling it
    directly would block the gateway heartbeat for the length of the request
    and Discord would drop the connection as unresponsive.

    `actor` is the Discord user id, carried through only so the audit row can
    say who asked. `can_draw` says whether the caller can render a chart.
    """
    # Checked before the model is called, not after: the point of a cap is
    # that the request over it is never paid for. On a worker thread because it
    # opens a database connection and this may be the gateway's event loop.
    allowance = await asyncio.to_thread(budget.check, actor, actor_kind)
    if not allowance.allowed:
        logger.warning(
            "%s/%s is over the daily cap: $%s of $%s",
            actor_kind, actor, allowance.spent, allowance.cap,
        )
        await asyncio.to_thread(
            record,
            kind="agent",
            operation=OPERATION,
            actor=actor,
            actor_kind=actor_kind,
            outcome="refused",
            detail={
                "reason": "daily spend cap",
                "spent_usd": float(allowance.spent),
                "cap_usd": float(allowance.cap),
                "surface": surface,
            },
        )
        return Reply(
            text=(
                "That would go over the daily spend cap — "
                f"{budget.usd(allowance.spent)} of {budget.usd(allowance.cap)} "
                "used in the last 24 hours. It frees up as those charges age "
                "out, or someone can raise DAILY_SPEND_CAP_USD."
            )
        )

    if not question.strip():
        return Reply(
            text=(
                "I got a mention with no text. If this keeps happening, message "
                "content may not be reaching me."
            )
        )

    started = time.perf_counter()
    try:
        reply = await asyncio.to_thread(_think, question, context, can_draw)
    except AiError as exc:
        logger.warning("agent reply failed: %s", exc)
        await asyncio.to_thread(
            record,
            kind="agent",
            operation=OPERATION,
            actor=actor,
            actor_kind=actor_kind,
            outcome="error",
            model=agent_model(),
            duration_ms=int((time.perf_counter() - started) * 1000),
            detail={"error": str(exc)[:200], "surface": surface},
        )
        return Reply(text="I could not reach the model just now.")

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "replied on %s: %d tokens, $%.5f", agent_model(), reply.tokens, reply.cost_usd
    )

    # Recorded on a worker thread for the same reason the model call is: this
    # opens a database connection, and the gateway heartbeat is on this loop.
    await asyncio.to_thread(
        record,
        kind="agent",
        operation=OPERATION,
        actor=actor,
        actor_kind=actor_kind,
        model=agent_model(),
        # The split is not reported per turn, so the whole conversation is
        # attributed to prompt tokens rather than invented as a ratio.
        prompt_tokens=reply.tokens,
        cost_usd=reply.cost_usd,
        duration_ms=elapsed_ms,
        detail={
            # Which surface the conversation happened on. The actor kind says
            # what sort of identity asked; this says where they were.
            "surface": surface,
            "tools": [t.name for t in reply.tools],
            "charts": [c.ticker for c in reply.charts],
            "chars": len(reply.text),
        },
    )

    if not reply.text:
        return Reply(
            text="The model returned nothing.", tools=reply.tools, charts=reply.charts
        )
    return Reply(
        text=_truncate(reply.text),
        tokens=reply.tokens,
        cost_usd=reply.cost_usd,
        tools=reply.tools,
        charts=reply.charts,
    )


def presence() -> str:
    """What Steven shows as his Discord status.

    The build and model used to ride on the end of every reply, which said the
    same thing every time and made a one-line answer look like a form. It is
    the same information, in the one place that is always visible and costs
    nothing to repeat.
    """
    return f"build {git_sha()[:7]} · {agent_model().split('/')[-1]}"
